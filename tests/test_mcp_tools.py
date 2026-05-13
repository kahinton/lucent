"""Tests for MCP tool functions in src/lucent/tools/memories.py.

Tests the MCP tool layer that wraps the DB layer, verifying:
- Input validation and error handling
- Auth context integration
- JSON serialization of responses
- Access control enforcement
"""

import json
import os
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from lucent.auth import set_current_user
from lucent.db import (
    MemoryRepository,
    UserRepository,
)
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
    set_current_user(
        {
            "id": test_user["id"],
            "organization_id": test_user["organization_id"],
            "role": "member",
            "display_name": "Test User",
            "email": "test@test.com",
        }
    )
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
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Test experience memory",
                "username": f"{prefix}user",
                "tags": ["test", "mcp"],
                "importance": 7,
            },
        )

        assert "id" in result
        assert result["type"] == "experience"
        assert result["content"] == f"{prefix} Test experience memory"
        assert sorted(result["tags"]) == ["mcp", "test"]
        assert result["importance"] == 7
        assert result["version"] == 1

    async def test_create_duplicate_technical_file_returns_update_instruction(
        self, mcp_tools, auth_user, clean_test_data
    ):
        prefix = clean_test_data
        first = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix}Technical note for memory.py",
                "tags": ["lucent", "database"],
                "metadata": {
                    "repo": "kahinton/lucent",
                    "filename": "src/lucent/db/memory.py",
                },
            },
        )
        assert "id" in first

        duplicate = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix}Duplicate note for memory.py",
                "tags": ["lucent", "database"],
                "metadata": {
                    "repo": "kahinton/lucent",
                    "directory": "src/lucent/db/",
                    "filename": "src/lucent/db/memory.py",
                },
            },
        )

        assert duplicate["error"]
        assert first["id"] in duplicate["error"]
        assert "Update that memory instead" in duplicate["error"]
        assert "intelligently combine" in duplicate["error"]

    async def test_create_retired_type_rejected(
        self, mcp_tools, auth_user, clean_test_data
    ):
        """Retired memory types cannot be newly created via MCP."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "procedural",
                "content": f"{prefix} Retired type memory",
                "username": f"{prefix}user",
            },
        )

        assert "error" in result
        assert "invalid memory type" in result["error"].lower()

    async def test_create_memory_invalid_type(self, mcp_tools, auth_user):
        """Test that an invalid memory type returns an error."""
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "invalid_type",
                "content": "Should fail",
            },
        )

        assert "error" in result

    async def test_create_individual_memory_rejected(self, mcp_tools, auth_user):
        """Test that individual memories cannot be created via MCP."""
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "individual",
                "content": "Should be rejected",
            },
        )

        assert "error" in result
        assert "Individual memories" in result["error"]

    async def test_create_memory_with_metadata(self, mcp_tools, auth_user, clean_test_data):
        """Test creating a memory with type-specific metadata."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Technical memory with metadata",
                "username": f"{prefix}user",
                "metadata": {"language": "python", "repo": "lucent"},
            },
        )

        assert "id" in result
        assert result["type"] == "technical"
        assert result["metadata"]["language"] == "python"

    async def test_create_technical_memory_without_anchor_rejected(
        self, mcp_tools, auth_user, clean_test_data
    ):
        """Technical memories need metadata that anchors them to a technical area."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Unanchored technical-ish note",
                "tags": ["daemon", "needs-review"],
            },
        )

        assert "error" in result
        assert "metadata.category" in result["error"]

    async def test_create_technical_memory_task_report_rejected(
        self, mcp_tools, auth_user, clean_test_data
    ):
        """Task reports and deliverable indexes should be experience/output artifacts."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": (
                    f"{prefix} Done.\n\n## Deliverables\n\n"
                    "- Branch: docs/example\n- Commit SHA: abc123"
                ),
                "tags": ["daemon", "needs-review"],
                "metadata": {"category": "architecture"},
            },
        )

        assert "error" in result
        assert "task report" in result["error"]

    async def test_create_memory_defaults(self, mcp_tools, auth_user, clean_test_data):
        """Test that default values are applied correctly."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Minimal memory",
                "username": f"{prefix}user",
            },
        )

        assert result["importance"] == 5
        assert result["tags"] == []
        assert result["related_memory_ids"] == []

    async def test_daemon_auto_sharing_via_mcp(self, mcp_tools, test_user, clean_test_data):
        """Daemon-service identity auto-shares memories even when shared=False."""
        prefix = clean_test_data
        set_current_user(
            {
                "id": test_user["id"],
                "organization_id": test_user["organization_id"],
                "role": "member",
                "display_name": "Lucent Daemon",
                "email": "daemon@test.com",
                "external_id": "daemon-service",
            }
        )
        try:
            result = await _call(
                mcp_tools,
                "create_memory",
                {
                    "type": "experience",
                    "content": f"{prefix} daemon MCP memory",
                    "username": f"{prefix}daemon",
                    "tags": ["test"],
                    "shared": False,
                },
            )
            assert result["shared"] is True
            assert "daemon" in result["tags"]
        finally:
            set_current_user(None)


# ============================================================================
# get_memory
# ============================================================================


class TestGetMemory:
    """Tests for the get_memory MCP tool."""

    async def test_get_existing_memory(self, mcp_tools, auth_user, test_memory):
        """Test retrieving a memory that exists."""
        result = await _call(
            mcp_tools,
            "get_memory",
            {
                "memory_id": str(test_memory["id"]),
            },
        )

        assert result["id"] == str(test_memory["id"])
        assert result["content"] == test_memory["content"]

    async def test_get_nonexistent_memory(self, mcp_tools, auth_user):
        """Test retrieving a memory that doesn't exist."""
        result = await _call(
            mcp_tools,
            "get_memory",
            {
                "memory_id": str(uuid4()),
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_get_memory_invalid_uuid(self, mcp_tools, auth_user):
        """Test retrieving with an invalid UUID format."""
        result = await _call(
            mcp_tools,
            "get_memory",
            {
                "memory_id": "not-a-uuid",
            },
        )

        assert "error" in result
        assert "Invalid" in result["error"]

    async def test_get_memory_repo_acl_blocks_when_access_denied(
        self, mcp_tools, auth_user, clean_test_data, monkeypatch
    ):
        prefix = clean_test_data
        created = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Private repo memory",
                "username": f"{prefix}user",
                "metadata": {"repo": "org/private-repo"},
            },
        )

        async def _deny_access(self, user_id, repo_full_name):  # pragma: no cover - signature shim
            return False

        monkeypatch.setattr(
            "lucent.integrations.github_repo_access_service.GitHubRepoAccessService.check_access",
            _deny_access,
        )

        result = await _call(
            mcp_tools,
            "get_memory",
            {
                "memory_id": created["id"],
            },
        )
        assert "error" in result
        assert "not found" in result["error"].lower()


# ============================================================================
# get_memories (batch)
# ============================================================================


class TestGetMemories:
    """Tests for the get_memories MCP tool."""

    async def test_get_single_memory(self, mcp_tools, auth_user, test_memory):
        """Test batch retrieval with a single ID."""
        result = await _call(
            mcp_tools,
            "get_memories",
            {
                "memory_ids": [str(test_memory["id"])],
            },
        )

        assert result["total_requested"] == 1
        assert result["total_found"] == 1
        assert len(result["memories"]) == 1
        assert result["memories"][0]["id"] == str(test_memory["id"])

    async def test_get_memories_mixed(self, mcp_tools, auth_user, test_memory):
        """Test batch retrieval with mix of found and not-found IDs."""
        fake_id = str(uuid4())
        result = await _call(
            mcp_tools,
            "get_memories",
            {
                "memory_ids": [str(test_memory["id"]), fake_id],
            },
        )

        assert result["total_requested"] == 2
        assert result["total_found"] == 1
        assert len(result["not_found"]) == 1
        assert fake_id in result["not_found"]

    async def test_get_memories_empty_list(self, mcp_tools, auth_user):
        """Test batch retrieval with empty list returns error."""
        result = await _call(
            mcp_tools,
            "get_memories",
            {
                "memory_ids": [],
            },
        )

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
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Python async programming patterns",
                "username": f"{prefix}user",
                "tags": ["python", "async"],
            },
        )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "query": f"{prefix} Python async",
            },
        )

        assert "memories" in result
        assert result["total_count"] >= 1
        assert "offset" in result
        assert "limit" in result
        assert "has_more" in result

    async def test_search_with_tag_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test searching with tag filter."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Tagged memory for search",
                "username": f"{prefix}user",
                "tags": ["unique-search-tag-xyz"],
                "metadata": {"category": "search-test"},
            },
        )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "tags": ["unique-search-tag-xyz"],
            },
        )

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert "unique-search-tag-xyz" in mem["tags"]

    async def test_search_with_type_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test searching with type filter."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Technical search content",
                "username": f"{prefix}user",
                "metadata": {"category": "search-test"},
            },
        )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "query": f"{prefix} Technical search",
                "type": "technical",
            },
        )

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert mem["type"] == "technical"

    async def test_search_repo_acl_filters_repo_tagged_results(
        self, mcp_tools, auth_user, clean_test_data, monkeypatch
    ):
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} private repo result",
                "username": f"{prefix}user",
                "metadata": {"repo": "org/private-repo"},
                "tags": ["acl", "private"],
            },
        )
        visible = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} visible result",
                "username": f"{prefix}user",
                "tags": ["acl", "visible"],
            },
        )

        async def _deny_access(self, user_id, repo_full_name):  # pragma: no cover - signature shim
            return False

        monkeypatch.setattr(
            "lucent.integrations.github_repo_access_service.GitHubRepoAccessService.check_access",
            _deny_access,
        )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "query": prefix,
                "limit": 20,
            },
        )

        ids = {m["id"] for m in result["memories"]}
        assert visible["id"] in ids
        assert all("private repo result" not in m["content"] for m in result["memories"])

    async def test_search_limit_and_offset(self, mcp_tools, auth_user, clean_test_data):
        """Test pagination with limit and offset."""
        prefix = clean_test_data
        # Create multiple memories
        for i in range(3):
            await _call(
                mcp_tools,
                "create_memory",
                {
                    "type": "experience",
                    "content": f"{prefix} Pagination test memory {i}",
                    "username": f"{prefix}user",
                    "tags": ["pagination-test"],
                },
            )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "tags": ["pagination-test"],
                "limit": 2,
                "offset": 0,
            },
        )

        assert len(result["memories"]) <= 2
        assert result["limit"] == 2
        assert result["offset"] == 0

    async def test_search_no_results(self, mcp_tools, auth_user):
        """Test search that returns no results."""
        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "query": "completely_nonexistent_query_string_xyz_12345",
            },
        )

        assert result["total_count"] == 0
        assert result["memories"] == []

    async def test_search_invalid_type(self, mcp_tools, auth_user):
        """Test search with invalid type filter."""
        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "type": "nonexistent_type",
            },
        )

        assert "error" in result

    async def test_search_content_truncation(self, mcp_tools, auth_user, clean_test_data):
        """Test that search results truncate long content."""
        prefix = clean_test_data
        long_content = f"{prefix} " + "x" * 2000
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": long_content,
                "username": f"{prefix}user",
                "tags": ["truncation-test"],
            },
        )

        result = await _call(
            mcp_tools,
            "search_memories",
            {
                "tags": ["truncation-test"],
            },
        )

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
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Full search test content",
                "username": f"{prefix}user",
                "tags": ["full-search-test"],
                "metadata": {"category": "search-test"},
            },
        )

        result = await _call(
            mcp_tools,
            "search_memories_full",
            {
                "query": f"{prefix} Full search test",
            },
        )

        assert "memories" in result
        assert result["total_count"] >= 1

    async def test_full_search_empty_query_rejected(self, mcp_tools, auth_user):
        """Test that empty query is rejected."""
        result = await _call(
            mcp_tools,
            "search_memories_full",
            {
                "query": "   ",
            },
        )

        assert "error" in result
        assert "required" in result["error"].lower()

    async def test_full_search_with_type_filter(
        self, mcp_tools, auth_user, clean_test_data, db_pool
    ):
        """Test full search with type filter."""
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)
        await repo.create(
            username=f"{prefix}user",
            type="procedural",
            content=f"{prefix} Procedural full search",
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
        )

        result = await _call(
            mcp_tools,
            "search_memories_full",
            {
                "query": f"{prefix} Procedural full search",
                "type": "procedural",
            },
        )

        assert result["total_count"] >= 1
        for mem in result["memories"]:
            assert mem["type"] == "procedural"


# ============================================================================
# include_archived (M9 Phase-2)
# ============================================================================


class TestSearchIncludeArchived:
    """``include_archived`` flag wiring through the MCP search tools."""

    async def _archive(self, db_pool, memory_id):
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET lifecycle_stage = 'archived' WHERE id = $1",
                memory_id,
            )

    async def test_search_memories_default_excludes_archived(
        self, mcp_tools, auth_user, clean_test_data, db_pool, monkeypatch
    ):
        monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
        prefix = clean_test_data

        active = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} mcp-include-archived ACTIVE",
                "username": f"{prefix}u",
            },
        )
        archived = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} mcp-include-archived ARCHIVED",
                "username": f"{prefix}u",
            },
        )
        await self._archive(db_pool, archived["id"])

        default = await _call(
            mcp_tools,
            "search_memories",
            {"query": f"{prefix} mcp-include-archived"},
        )
        ids = {m["id"] for m in default["memories"]}
        assert active["id"] in ids
        assert archived["id"] not in ids

        inclusive = await _call(
            mcp_tools,
            "search_memories",
            {
                "query": f"{prefix} mcp-include-archived",
                "include_archived": True,
            },
        )
        ids = {m["id"] for m in inclusive["memories"]}
        assert archived["id"] in ids

    async def test_search_memories_full_default_excludes_archived(
        self, mcp_tools, auth_user, clean_test_data, db_pool, monkeypatch
    ):
        monkeypatch.setenv("LUCENT_SEARCH_EXCLUDE_ARCHIVED_ENABLED", "true")
        prefix = clean_test_data

        active = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} mcp-full-archived ACTIVE row",
                "username": f"{prefix}u",
                "metadata": {"category": "search-test"},
            },
        )
        archived = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} mcp-full-archived ARCHIVED row",
                "username": f"{prefix}u",
                "metadata": {"category": "search-test"},
            },
        )
        await self._archive(db_pool, archived["id"])

        default = await _call(
            mcp_tools,
            "search_memories_full",
            {"query": f"{prefix} mcp-full-archived"},
        )
        ids = {m["id"] for m in default["memories"]}
        assert active["id"] in ids
        assert archived["id"] not in ids

        inclusive = await _call(
            mcp_tools,
            "search_memories_full",
            {
                "query": f"{prefix} mcp-full-archived",
                "include_archived": True,
            },
        )
        ids = {m["id"] for m in inclusive["memories"]}
        assert archived["id"] in ids


# ============================================================================
# update_memory
# ============================================================================


class TestUpdateMemory:
    """Tests for the update_memory MCP tool."""

    async def test_update_content(self, mcp_tools, auth_user, test_memory):
        """Test updating memory content."""
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "content": "Updated content via MCP",
            },
        )

        assert result["content"] == "Updated content via MCP"
        assert result["version"] == 2

    async def test_update_tags_and_importance(self, mcp_tools, auth_user, test_memory):
        """Test updating tags and importance."""
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "tags": ["updated", "mcp-test"],
                "importance": 9,
            },
        )

        assert sorted(result["tags"]) == ["mcp-test", "updated"]
        assert result["importance"] == 9

    async def test_update_nonexistent_memory(self, mcp_tools, auth_user):
        """Test updating a memory that doesn't exist."""
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(uuid4()),
                "content": "Should fail",
            },
        )

        assert "error" in result

    async def test_update_with_expected_version_success(self, mcp_tools, auth_user, test_memory):
        """Test optimistic locking with correct expected_version."""
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "content": "Version-checked update",
                "expected_version": test_memory["version"],
            },
        )

        assert result["content"] == "Version-checked update"
        assert result["version"] == test_memory["version"] + 1

    async def test_update_with_expected_version_conflict(self, mcp_tools, auth_user, test_memory):
        """Test optimistic locking with wrong expected_version."""
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "content": "Should conflict",
                "expected_version": 999,
            },
        )

        assert "error" in result
        assert "Version conflict" in result["error"] or "version" in result["error"].lower()

    async def test_update_requires_auth(self, mcp_tools, test_memory):
        """Test that update requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "content": "No auth",
            },
        )

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()

    async def test_update_ownership_check(
        self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data
    ):
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
        set_current_user(
            {
                "id": other_user["id"],
                "organization_id": other_user["organization_id"],
                "role": "member",
            }
        )

        result = await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": str(test_memory["id"]),
                "content": "Should not work",
            },
        )

        assert "error" in result
        assert "not accessible" in result["error"].lower() or "Permission denied" in result["error"]
        set_current_user(None)


# ============================================================================
# pin_memory / unpin_memory
# ============================================================================


class TestPinUnpinMemory:
    """Tests for the pin_memory / unpin_memory MCP tools."""

    async def test_pin_adds_pinned_tag_and_bumps_version(
        self, mcp_tools, auth_user, test_memory
    ):
        result = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" not in result, result
        assert "pinned" in result["tags"]
        # other tags preserved
        assert "test" in result["tags"]
        assert "fixture" in result["tags"]
        assert result["version"] == 2

    async def test_pin_is_idempotent(self, mcp_tools, auth_user, test_memory):
        first = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])}
        )
        second = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert first["tags"].count("pinned") == 1
        assert second["tags"].count("pinned") == 1
        # Version bumps both times (matches update_memory semantics).
        assert second["version"] == first["version"] + 1

    async def test_unpin_removes_pinned_tag(self, mcp_tools, auth_user, test_memory):
        await _call(mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])})
        result = await _call(
            mcp_tools, "unpin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" not in result, result
        assert "pinned" not in result["tags"]
        assert "test" in result["tags"]

    async def test_unpin_is_idempotent(self, mcp_tools, auth_user, test_memory):
        # Memory was never pinned — unpin should still succeed.
        result = await _call(
            mcp_tools, "unpin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" not in result, result
        assert "pinned" not in result["tags"]

    async def test_pin_nonexistent_memory(self, mcp_tools, auth_user):
        result = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(uuid4())}
        )
        assert "error" in result
        assert "not accessible" in result["error"].lower() or "not found" in result["error"].lower()

    async def test_unpin_nonexistent_memory(self, mcp_tools, auth_user):
        result = await _call(
            mcp_tools, "unpin_memory", {"memory_id": str(uuid4())}
        )
        assert "error" in result

    async def test_pin_requires_auth(self, mcp_tools, test_memory):
        set_current_user(None)
        result = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" in result
        assert "auth" in result["error"].lower()

    async def test_unpin_requires_auth(self, mcp_tools, test_memory):
        set_current_user(None)
        result = await _call(
            mcp_tools, "unpin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" in result
        assert "auth" in result["error"].lower()

    async def test_pin_acl_blocks_non_owner(
        self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data
    ):
        """Non-owner in same org cannot pin another user's private memory."""
        prefix = clean_test_data

        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{prefix}pin_other",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}pin_other@test.com",
            display_name=f"{prefix}Pin Other",
        )
        set_current_user(
            {
                "id": other_user["id"],
                "organization_id": other_user["organization_id"],
                "role": "member",
            }
        )

        result = await _call(
            mcp_tools, "pin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" in result
        err = result["error"].lower()
        assert "not accessible" in err or "permission denied" in err
        set_current_user(None)

    async def test_unpin_acl_blocks_non_owner(
        self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data
    ):
        prefix = clean_test_data

        user_repo = UserRepository(db_pool)
        other_user = await user_repo.create(
            external_id=f"{prefix}unpin_other",
            provider="local",
            organization_id=test_organization["id"],
            email=f"{prefix}unpin_other@test.com",
            display_name=f"{prefix}Unpin Other",
        )
        set_current_user(
            {
                "id": other_user["id"],
                "organization_id": other_user["organization_id"],
                "role": "member",
            }
        )

        result = await _call(
            mcp_tools, "unpin_memory", {"memory_id": str(test_memory["id"])}
        )
        assert "error" in result
        err = result["error"].lower()
        assert "not accessible" in err or "permission denied" in err
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
        created = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Memory to delete",
                "username": f"{prefix}user",
            },
        )
        memory_id = created["id"]

        result = await _call(
            mcp_tools,
            "delete_memory",
            {
                "memory_id": memory_id,
            },
        )

        assert result["success"] is True

        # Verify it's no longer retrievable
        get_result = await _call(
            mcp_tools,
            "get_memory",
            {
                "memory_id": memory_id,
            },
        )
        assert "error" in get_result

    async def test_soft_delete_flag_off_has_no_ldr_sidecar_write(
        self, mcp_tools, auth_user, db_pool, clean_test_data, monkeypatch
    ):
        """Flag OFF keeps delete behavior and emits no LDR observation rows."""
        monkeypatch.delenv("LUCENT_SHADOW_FORGET_ENABLED", raising=False)
        prefix = clean_test_data

        created = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Memory to delete no ldr row",
                "username": f"{prefix}user",
            },
        )
        memory_id = created["id"]

        result = await _call(mcp_tools, "delete_memory", {"memory_id": memory_id})
        assert result["success"] is True

        get_result = await _call(mcp_tools, "get_memory", {"memory_id": memory_id})
        assert "error" in get_result

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM memory_shadow_scores
                WHERE memory_id = $1
                  AND strategy = 'ldr-obs-v1'
                """,
                UUID(memory_id),
            )
        assert count == 0

    async def test_soft_delete_flag_on_writes_ldr_row_with_replacement_metadata(
        self, mcp_tools, auth_user, db_pool, clean_test_data, monkeypatch
    ):
        """Flag ON writes LDR observation row with edges-at-risk and canonical metadata."""
        monkeypatch.setenv("LUCENT_SHADOW_FORGET_ENABLED", "true")
        prefix = clean_test_data
        repo = MemoryRepository(db_pool)

        canonical = await repo.create(
            username=f"{prefix}user",
            type="technical",
            content=f"{prefix} canonical target",
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
        )
        source = await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} source to delete",
            metadata={"canonical_memory_id": str(canonical["id"])},
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
        )
        await repo.create(
            username=f"{prefix}user",
            type="experience",
            content=f"{prefix} inbound edge source",
            related_memory_ids=[source["id"]],
            user_id=auth_user["id"],
            organization_id=auth_user["organization_id"],
        )

        result = await _call(
            mcp_tools,
            "delete_memory",
            {"memory_id": str(source["id"])},
        )
        assert result["success"] is True

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT shadow_action, signals
                FROM memory_shadow_scores
                WHERE memory_id = $1
                  AND strategy = 'ldr-obs-v1'
                ORDER BY computed_at DESC
                LIMIT 1
                """,
                source["id"],
            )
        assert row is not None
        assert row["shadow_action"] == "would_demote"
        assert row["signals"]["would_demote_source_id"] == str(source["id"])
        assert row["signals"]["would_link_canonical_id"] == str(canonical["id"])
        assert row["signals"]["would_break_edges"] == 1
        assert row["signals"]["force_delete_compliance"] is False

    async def test_delete_nonexistent(self, mcp_tools, auth_user):
        """Test deleting a memory that doesn't exist."""
        result = await _call(
            mcp_tools,
            "delete_memory",
            {
                "memory_id": str(uuid4()),
            },
        )

        assert "error" in result

    async def test_delete_requires_auth(self, mcp_tools, test_memory):
        """Test that delete requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "delete_memory",
            {
                "memory_id": str(test_memory["id"]),
            },
        )

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()

    async def test_delete_individual_memory_rejected(
        self, mcp_tools, db_pool, auth_user, clean_test_data
    ):
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

        result = await _call(
            mcp_tools,
            "delete_memory",
            {
                "memory_id": str(memory["id"]),
            },
        )

        assert "error" in result
        assert "Individual memories" in result["error"]

    async def test_delete_ownership_check(
        self, mcp_tools, db_pool, test_memory, test_organization, clean_test_data
    ):
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

        set_current_user(
            {
                "id": other_user["id"],
                "organization_id": other_user["organization_id"],
                "role": "member",
            }
        )

        result = await _call(
            mcp_tools,
            "delete_memory",
            {
                "memory_id": str(test_memory["id"]),
            },
        )

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
            await _call(
                mcp_tools,
                "create_memory",
                {
                    "type": "experience",
                    "content": f"{prefix} Tag count test",
                    "username": f"{prefix}user",
                    "tags": ["tag-count-test-abc"],
                },
            )

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
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Tech tag test",
                "username": f"{prefix}user",
                "tags": ["tech-tag-filter-test"],
                "metadata": {"category": "tag-test"},
            },
        )

        result = await _call(
            mcp_tools,
            "get_existing_tags",
            {
                "type": "technical",
            },
        )

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


# ============================================================================
# get_tag_suggestions
# ============================================================================


class TestGetTagSuggestions:
    """Tests for the get_tag_suggestions MCP tool."""

    async def test_get_suggestions(self, mcp_tools, auth_user, clean_test_data):
        """Test getting tag suggestions for an existing tag prefix."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Tag suggestion test",
                "username": f"{prefix}user",
                "tags": ["suggestion-target-abc"],
            },
        )

        result = await _call(
            mcp_tools,
            "get_tag_suggestions",
            {
                "query": "suggestion-target",
            },
        )

        assert "suggestions" in result
        assert result["query"] == "suggestion-target"
        assert "total_returned" in result

    async def test_empty_query_rejected(self, mcp_tools, auth_user):
        """Test that empty query returns an error."""
        result = await _call(
            mcp_tools,
            "get_tag_suggestions",
            {
                "query": "   ",
            },
        )

        assert "error" in result
        assert "required" in result["error"].lower()

    async def test_limit_capped(self, mcp_tools, auth_user, clean_test_data):
        """Test that limit is capped at 25."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Limit cap test",
                "username": f"{prefix}user",
                "tags": ["limit-cap-tag"],
            },
        )

        result = await _call(
            mcp_tools,
            "get_tag_suggestions",
            {
                "query": "limit",
                "limit": 100,
            },
        )

        assert "suggestions" in result


# ============================================================================
# get_memory_versions
# ============================================================================


class TestGetMemoryVersions:
    """Tests for the get_memory_versions MCP tool."""

    async def test_get_versions_for_updated_memory(self, mcp_tools, auth_user, test_memory):
        """Test retrieving version history after an update."""
        memory_id = str(test_memory["id"])

        # Update to create version history
        await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": memory_id,
                "content": "Updated for version test",
            },
        )

        result = await _call(
            mcp_tools,
            "get_memory_versions",
            {
                "memory_id": memory_id,
            },
        )

        assert "versions" in result
        assert result["memory_id"] == memory_id
        assert "current_version" in result
        assert "total_count" in result
        assert "has_more" in result

    async def test_get_versions_nonexistent_memory(self, mcp_tools, auth_user):
        """Test getting versions for a nonexistent memory."""
        result = await _call(
            mcp_tools,
            "get_memory_versions",
            {
                "memory_id": str(uuid4()),
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower() or "not accessible" in result["error"].lower()

    async def test_get_versions_invalid_uuid(self, mcp_tools, auth_user):
        """Test getting versions with invalid UUID."""
        result = await _call(
            mcp_tools,
            "get_memory_versions",
            {
                "memory_id": "bad-uuid",
            },
        )

        assert "error" in result
        assert "Invalid" in result["error"]

    async def test_get_versions_requires_auth(self, mcp_tools, test_memory):
        """Test that getting versions requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "get_memory_versions",
            {
                "memory_id": str(test_memory["id"]),
            },
        )

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()


# ============================================================================
# restore_memory_version
# ============================================================================


class TestRestoreMemoryVersion:
    """Tests for the restore_memory_version MCP tool."""

    async def test_restore_requires_auth(self, mcp_tools, test_memory):
        """Test that restore requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "restore_memory_version",
            {
                "memory_id": str(test_memory["id"]),
                "version": 1,
            },
        )

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()

    async def test_restore_nonexistent_memory(self, mcp_tools, auth_user):
        """Test restoring a nonexistent memory."""
        result = await _call(
            mcp_tools,
            "restore_memory_version",
            {
                "memory_id": str(uuid4()),
                "version": 1,
            },
        )

        assert "error" in result

    async def test_restore_invalid_uuid(self, mcp_tools, auth_user):
        """Test restoring with invalid UUID format."""
        result = await _call(
            mcp_tools,
            "restore_memory_version",
            {
                "memory_id": "not-a-uuid",
                "version": 1,
            },
        )

        assert "error" in result
        assert "Invalid" in result["error"]

    async def test_restore_same_version_rejected(self, mcp_tools, auth_user, test_memory):
        """Test that restoring to the current version is rejected."""
        result = await _call(
            mcp_tools,
            "restore_memory_version",
            {
                "memory_id": str(test_memory["id"]),
                "version": test_memory["version"],
            },
        )

        assert "error" in result
        assert "already at version" in result["error"].lower()

    async def test_restore_to_previous_version(self, mcp_tools, auth_user, clean_test_data):
        """Test successfully restoring a memory to a previous version."""
        prefix = clean_test_data
        # Create via MCP so audit log/snapshot exists for version 1
        created = await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Original content for restore test",
                "username": f"{prefix}user",
            },
        )
        memory_id = created["id"]
        original_content = created["content"]

        # Update the memory to create version 2
        await _call(
            mcp_tools,
            "update_memory",
            {
                "memory_id": memory_id,
                "content": "Version 2 content",
            },
        )

        # Verify it was updated
        updated = await _call(mcp_tools, "get_memory", {"memory_id": memory_id})
        assert updated["content"] == "Version 2 content"
        assert updated["version"] == 2

        # Restore to version 1
        result = await _call(
            mcp_tools,
            "restore_memory_version",
            {
                "memory_id": memory_id,
                "version": 1,
            },
        )

        assert "error" not in result
        assert result["content"] == original_content
        assert result["version"] == 3  # restore creates a new version
        assert result["restored_from_version"] == 1


# ============================================================================
# create_daemon_task
# ============================================================================


class TestCreateDaemonTask:
    """Tests for the create_daemon_task MCP tool."""

    async def test_create_basic_task(self, mcp_tools, auth_user, clean_test_data):
        """Test creating a basic daemon task."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Review the auth module",
            },
        )

        assert "id" in result
        assert "daemon-task" in result["tags"]
        assert "pending" in result["tags"]
        assert "code" in result["tags"]  # default agent_type
        assert "medium" in result["tags"]  # default priority

    async def test_create_task_with_options(self, mcp_tools, auth_user, clean_test_data):
        """Test creating a daemon task with custom options."""
        prefix = clean_test_data
        result = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Research API patterns",
                "agent_type": "research",
                "priority": "high",
                "context": "Focus on REST best practices",
                "tags": ["api"],
            },
        )

        assert "id" in result
        assert "research" in result["tags"]
        assert "high" in result["tags"]
        assert "api" in result["tags"]
        assert result["importance"] == 8  # high priority = 8

    async def test_create_task_invalid_agent_type(self, mcp_tools, auth_user):
        """Test that invalid agent_type returns an error."""
        result = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": "Should fail",
                "agent_type": "invalid",
            },
        )

        assert "error" in result
        assert "agent_type" in result["error"].lower()

    async def test_create_task_invalid_priority(self, mcp_tools, auth_user):
        """Test that invalid priority returns an error."""
        result = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": "Should fail",
                "priority": "critical",
            },
        )

        assert "error" in result
        assert "priority" in result["error"].lower()

    async def test_create_task_requires_auth(self, mcp_tools):
        """Test that task creation requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": "No auth task",
            },
        )

        assert "error" in result
        assert "Authentication" in result["error"] or "auth" in result["error"].lower()


# ============================================================================
# claim_task
# ============================================================================


class TestClaimTask:
    """Tests for the claim_task MCP tool."""

    async def test_claim_pending_task(self, mcp_tools, auth_user, clean_test_data):
        """Test claiming a pending daemon task."""
        prefix = clean_test_data
        task = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Task to claim",
            },
        )

        result = await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": task["id"],
                "instance_id": "test-instance-1",
            },
        )

        assert "id" in result
        assert "claimed-by-test-instance-1" in result["tags"]
        assert "pending" not in result["tags"]

    async def test_claim_already_claimed(self, mcp_tools, auth_user, clean_test_data):
        """Test claiming a task that is already claimed."""
        prefix = clean_test_data
        task = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Task double claim",
            },
        )

        # First claim succeeds
        await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": task["id"],
                "instance_id": "instance-a",
            },
        )

        # Second claim fails
        result = await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": task["id"],
                "instance_id": "instance-b",
            },
        )

        assert "error" in result

    async def test_claim_invalid_uuid(self, mcp_tools, auth_user):
        """Test claiming with invalid UUID."""
        result = await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": "not-a-uuid",
                "instance_id": "test-instance",
            },
        )

        assert "error" in result

    async def test_claim_requires_auth(self, mcp_tools):
        """Test that claiming requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": str(uuid4()),
                "instance_id": "test-instance",
            },
        )

        assert "error" in result


# ============================================================================
# release_claim
# ============================================================================


class TestReleaseClaim:
    """Tests for the release_claim MCP tool."""

    async def test_release_claimed_task(self, mcp_tools, auth_user, clean_test_data):
        """Test releasing a claimed task back to pending."""
        prefix = clean_test_data
        task = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Task to release",
            },
        )

        await _call(
            mcp_tools,
            "claim_task",
            {
                "memory_id": task["id"],
                "instance_id": "release-instance",
            },
        )

        result = await _call(
            mcp_tools,
            "release_claim",
            {
                "memory_id": task["id"],
                "instance_id": "release-instance",
            },
        )

        assert "id" in result
        assert "pending" in result["tags"]

    async def test_release_unclaimed_task(self, mcp_tools, auth_user, clean_test_data):
        """Test releasing a task that is not claimed."""
        prefix = clean_test_data
        task = await _call(
            mcp_tools,
            "create_daemon_task",
            {
                "description": f"{prefix} Not claimed task",
            },
        )

        result = await _call(
            mcp_tools,
            "release_claim",
            {
                "memory_id": task["id"],
            },
        )

        assert "error" in result

    async def test_release_requires_auth(self, mcp_tools):
        """Test that releasing requires authentication."""
        set_current_user(None)
        result = await _call(
            mcp_tools,
            "release_claim",
            {
                "memory_id": str(uuid4()),
            },
        )

        assert "error" in result


# ============================================================================
# export_memories
# ============================================================================


class TestExportMemories:
    """Tests for the export_memories MCP tool."""

    async def test_export_all(self, mcp_tools, auth_user, clean_test_data):
        """Test exporting all memories."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Export test memory",
                "username": f"{prefix}user",
                "tags": ["export-test"],
            },
        )

        result = await _call(mcp_tools, "export_memories", {})

        assert "metadata" in result
        assert "memories" in result
        assert result["metadata"]["total_count"] >= 1
        assert result["metadata"]["format"] == "json"
        assert "exported_at" in result["metadata"]

    async def test_export_with_type_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test exporting with type filter."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Technical export test",
                "username": f"{prefix}user",
            },
        )

        result = await _call(
            mcp_tools,
            "export_memories",
            {
                "type": "technical",
            },
        )

        assert "memories" in result
        for mem in result["memories"]:
            assert mem["type"] == "technical"
        assert result["metadata"]["filters"].get("type") == "technical"

    async def test_export_with_tag_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test exporting with tag filter."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} Tag export test",
                "username": f"{prefix}user",
                "tags": ["unique-export-tag-xyz"],
            },
        )

        result = await _call(
            mcp_tools,
            "export_memories",
            {
                "tags": ["unique-export-tag-xyz"],
            },
        )

        assert result["metadata"]["total_count"] >= 1

    async def test_export_with_importance_filter(self, mcp_tools, auth_user, clean_test_data):
        """Test exporting with importance range filter."""
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} High importance export",
                "username": f"{prefix}user",
                "importance": 9,
                "tags": ["importance-export-test"],
            },
        )

        result = await _call(
            mcp_tools,
            "export_memories",
            {
                "importance_min": 8,
                "importance_max": 10,
            },
        )

        assert "memories" in result
        for mem in result["memories"]:
            assert mem["importance"] >= 8

    async def test_export_repo_acl_filters_repo_tagged_results(
        self, mcp_tools, auth_user, clean_test_data, monkeypatch
    ):
        prefix = clean_test_data
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Private repo export",
                "username": f"{prefix}user",
                "metadata": {"repo": "org/private-repo"},
                "tags": ["acl-export", "private"],
            },
        )
        await _call(
            mcp_tools,
            "create_memory",
            {
                "type": "technical",
                "content": f"{prefix} Untagged export",
                "username": f"{prefix}user",
                "tags": ["acl-export", "visible"],
                "metadata": {"category": "export-test"},
            },
        )

        async def _deny_access(self, user_id, repo_full_name):  # pragma: no cover - signature shim
            return False

        monkeypatch.setattr(
            "lucent.integrations.github_repo_access_service.GitHubRepoAccessService.check_access",
            _deny_access,
        )

        result = await _call(
            mcp_tools,
            "export_memories",
            {"tags": ["acl-export"]},
        )

        contents = {m["content"] for m in result["memories"]}
        assert f"{prefix} Private repo export" not in contents
        assert f"{prefix} Untagged export" in contents


# ============================================================================
# import_memories
# ============================================================================


class TestImportMemories:
    """Tests for the import_memories MCP tool."""

    async def test_import_from_list(self, mcp_tools, auth_user, clean_test_data):
        """Test importing from a JSON list of memories."""
        import json as _json

        prefix = clean_test_data
        memories_data = [
            {
                "type": "experience",
                "content": f"{prefix} Imported memory 1",
                "tags": ["import-test"],
                "importance": 5,
            },
            {
                "type": "technical",
                "content": f"{prefix} Imported memory 2",
                "tags": ["import-test"],
                "importance": 6,
            },
        ]

        result = await _call(
            mcp_tools,
            "import_memories",
            {
                "memories_json": _json.dumps(memories_data),
            },
        )

        assert "imported" in result or "total" in result

    async def test_import_from_export_object(self, mcp_tools, auth_user, clean_test_data):
        """Test importing from an export-format object with 'memories' key."""
        import json as _json

        prefix = clean_test_data
        export_data = {
            "metadata": {"format": "json"},
            "memories": [
                {
                    "type": "experience",
                    "content": f"{prefix} Export-format import",
                    "tags": ["import-export-test"],
                    "importance": 5,
                },
            ],
        }

        result = await _call(
            mcp_tools,
            "import_memories",
            {
                "memories_json": _json.dumps(export_data),
            },
        )

        assert "error" not in result

    async def test_import_invalid_json(self, mcp_tools, auth_user):
        """Test importing invalid JSON."""
        result = await _call(
            mcp_tools,
            "import_memories",
            {
                "memories_json": "not valid json{{{",
            },
        )

        assert "error" in result
        assert "JSON" in result["error"]

    async def test_import_invalid_structure(self, mcp_tools, auth_user):
        """Test importing with invalid structure (not a list or export object)."""
        import json as _json

        result = await _call(
            mcp_tools,
            "import_memories",
            {
                "memories_json": _json.dumps({"wrong": "structure"}),
            },
        )

        assert "error" in result


# ============================================================================
# get_memory_stats
# ============================================================================


class TestLifecycleToolsNotExposed:
    """Lifecycle/stat maintenance routines must not be registered as MCP tools."""

    def test_lifecycle_tools_are_not_public_mcp_tools(self, mcp_tools):
        registered = set(mcp_tools._tool_manager._tools)
        assert "get_memory_stats" not in registered
        assert "compute_vitality_scores" not in registered
        assert "compute_shadow_forget_scores" not in registered
        assert "get_shadow_forget_comparison" not in registered

# ============================================================================
# Team-mode fixtures and tests for share_memory / unshare_memory
# ============================================================================


@pytest_asyncio.fixture
async def team_mcp_tools(db_pool):
    """Create a FastMCP instance with team mode enabled (registers share/unshare tools)."""
    import lucent.mode as mode_module

    old_mode = os.environ.get("LUCENT_MODE")
    old_license = os.environ.get("LUCENT_LICENSE_KEY")
    os.environ["LUCENT_MODE"] = "team"
    os.environ["LUCENT_LICENSE_KEY"] = "test-license-key"
    mode_module.get_mode.cache_clear()

    with patch("lucent.mode._validate_license", return_value=True):
        mode_module.get_mode.cache_clear()
        mcp = FastMCP("test-team")
        register_tools(mcp)

    yield mcp

    # Restore original env
    if old_mode is None:
        os.environ.pop("LUCENT_MODE", None)
    else:
        os.environ["LUCENT_MODE"] = old_mode
    if old_license is None:
        os.environ.pop("LUCENT_LICENSE_KEY", None)
    else:
        os.environ["LUCENT_LICENSE_KEY"] = old_license
    mode_module.get_mode.cache_clear()


class TestShareMemory:
    """Tests for the share_memory MCP tool (team mode only)."""

    async def test_share_memory_success(self, team_mcp_tools, auth_user, clean_test_data):
        """Test sharing a memory sets shared=true."""
        prefix = clean_test_data
        created = await _call(
            team_mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} shareable memory",
                "username": f"{prefix}user",
                "tags": ["test"],
            },
        )
        assert "id" in created

        result = await _call(
            team_mcp_tools,
            "share_memory",
            {
                "memory_id": created["id"],
            },
        )

        assert "error" not in result
        assert result.get("shared") is True

    async def test_share_memory_requires_auth(self, team_mcp_tools, test_memory):
        """Test that share_memory requires authentication."""
        set_current_user(None)
        result = await _call(
            team_mcp_tools,
            "share_memory",
            {
                "memory_id": str(test_memory["id"]),
            },
        )
        assert "error" in result
        assert "Authentication required" in result["error"]

    async def test_share_memory_nonexistent(self, team_mcp_tools, auth_user):
        """Test sharing a nonexistent memory returns error."""
        result = await _call(
            team_mcp_tools,
            "share_memory",
            {
                "memory_id": str(uuid4()),
            },
        )
        assert "error" in result
        assert "not found" in result["error"].lower() or "not the owner" in result["error"].lower()

    async def test_share_memory_invalid_uuid(self, team_mcp_tools, auth_user):
        """Test sharing with invalid UUID returns error."""
        result = await _call(
            team_mcp_tools,
            "share_memory",
            {
                "memory_id": "not-a-uuid",
            },
        )
        assert "error" in result


class TestUnshareMemory:
    """Tests for the unshare_memory MCP tool (team mode only)."""

    async def test_unshare_memory_success(self, team_mcp_tools, auth_user, clean_test_data):
        """Test unsharing a shared memory sets shared=false."""
        prefix = clean_test_data
        created = await _call(
            team_mcp_tools,
            "create_memory",
            {
                "type": "experience",
                "content": f"{prefix} unshareable memory",
                "username": f"{prefix}user",
                "tags": ["test"],
            },
        )

        # Share first
        await _call(
            team_mcp_tools,
            "share_memory",
            {
                "memory_id": created["id"],
            },
        )

        # Then unshare
        result = await _call(
            team_mcp_tools,
            "unshare_memory",
            {
                "memory_id": created["id"],
            },
        )

        assert "error" not in result
        assert result.get("shared") is False

    async def test_unshare_memory_requires_auth(self, team_mcp_tools, test_memory):
        """Test that unshare_memory requires authentication."""
        set_current_user(None)
        result = await _call(
            team_mcp_tools,
            "unshare_memory",
            {
                "memory_id": str(test_memory["id"]),
            },
        )
        assert "error" in result
        assert "Authentication required" in result["error"]

    async def test_unshare_memory_nonexistent(self, team_mcp_tools, auth_user):
        """Test unsharing a nonexistent memory returns error."""
        result = await _call(
            team_mcp_tools,
            "unshare_memory",
            {
                "memory_id": str(uuid4()),
            },
        )
        assert "error" in result

    async def test_unshare_memory_invalid_uuid(self, team_mcp_tools, auth_user):
        """Test unsharing with invalid UUID returns error."""
        result = await _call(
            team_mcp_tools,
            "unshare_memory",
            {
                "memory_id": "not-a-uuid",
            },
        )
        assert "error" in result
