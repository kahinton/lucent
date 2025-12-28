"""MCP tools for memory CRUD operations."""

import json
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from hindsight.auth import ensure_dev_user, get_current_user, is_dev_mode
from hindsight.db.client import MemoryRepository, UserRepository, get_pool, init_db
from hindsight.models.memory import (
    CreateMemoryInput,
    MemoryType,
    SearchMemoriesInput,
    UpdateMemoryInput,
)


async def _get_repository() -> MemoryRepository:
    """Get a memory repository, initializing the database if needed."""
    try:
        pool = await get_pool()
    except RuntimeError:
        # Pool not initialized yet, initialize it now
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        pool = await init_db(database_url)
    return MemoryRepository(pool)


async def _get_current_user_id() -> UUID | None:
    """Get the current user ID, creating dev user if in dev mode."""
    # Check if we have a user in context (set by auth middleware)
    current_user = get_current_user()
    if current_user:
        return current_user["id"]
    
    # In dev mode, ensure dev user exists and use it
    if is_dev_mode():
        dev_user = await ensure_dev_user()
        return dev_user["id"]
    
    # No user context and not in dev mode
    return None


async def _get_repository() -> MemoryRepository:
    """Get a memory repository, initializing the database if needed."""
    try:
        pool = await get_pool()
    except RuntimeError:
        # Pool not initialized yet, initialize it now
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        pool = await init_db(database_url)
    return MemoryRepository(pool)


def register_tools(mcp: FastMCP) -> None:
    """Register all memory tools with the MCP server."""

    @mcp.tool()
    async def create_memory(
        username: str,
        type: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 5,
        related_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new memory in the knowledge base.

        Args:
            username: Username of the person this memory is being created for (required).
            type: Type of memory - one of: experience, technical, procedural, goal, individual.
            content: The main content/description of the memory.
            tags: Optional list of tags for categorization.
            importance: Importance rating from 1 (routine) to 10 (essential). Default is 5.
            related_memory_ids: Optional list of UUIDs of related memories to link.
            metadata: Optional type-specific metadata. Structure depends on memory type:
                - experience: {context, outcome, lessons_learned[], related_entities[]}
                - technical: {category, language, code_snippet, references[], version_info, repo, filename}
                - procedural: {steps[{order, description, notes}], prerequisites[], estimated_time, success_criteria, common_pitfalls[]}
                - goal: {status, deadline, milestones[{description, status, completed_at}], blockers[], progress_notes[{date, note}], priority}
                - individual: {name, relationship, organization, role, contact_info{email, phone, linkedin, github, other}, preferences[], interaction_history[{date, context, notes}], last_interaction}

        Returns:
            JSON string with the created memory including its ID.
        """
        try:
            # Validate input
            memory_type = MemoryType(type)
            
            input_data = CreateMemoryInput(
                username=username,
                type=memory_type,
                content=content,
                tags=tags or [],
                importance=importance,
                related_memory_ids=[UUID(uid) for uid in (related_memory_ids or [])],
                metadata=metadata or {},
            )
            
            # Get current user ID (from auth context or dev mode)
            user_id = await _get_current_user_id()
            
            repo = await _get_repository()
            
            result = await repo.create(
                username=input_data.username,
                type=input_data.type.value,
                content=input_data.content,
                tags=input_data.tags,
                importance=input_data.importance,
                related_memory_ids=input_data.related_memory_ids,
                metadata=input_data.metadata,
                user_id=user_id,
            )
            
            return json.dumps(_serialize_memory(result), indent=2)
            
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            return json.dumps({"error": f"Failed to create memory: {str(e)}"})

    @mcp.tool()
    async def get_memory(memory_id: str) -> str:
        """Retrieve a memory by its ID.

        Args:
            memory_id: The UUID of the memory to retrieve.

        Returns:
            JSON string with the full memory details, or an error if not found.
        """
        try:
            uuid_id = UUID(memory_id)
            
            repo = await _get_repository()
            
            result = await repo.get(uuid_id)
            
            if result is None:
                return json.dumps({"error": f"Memory not found: {memory_id}"})
            
            return json.dumps(_serialize_memory(result), indent=2)
            
        except ValueError as e:
            return json.dumps({"error": f"Invalid memory ID format: {str(e)}"})
        except Exception as e:
            return json.dumps({"error": f"Failed to retrieve memory: {str(e)}"})

    @mcp.tool()
    async def search_memories(
        query: str | None = None,
        username: str | None = None,
        type: str | None = None,
        tags: list[str] | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        memory_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 5,
    ) -> str:
        """Search for memories by content with fuzzy matching and filters.

        This searches the main CONTENT field only. For searching across all fields
        (content, tags, metadata), use search_memories_full instead.

        Args:
            query: Optional fuzzy search query to match against memory CONTENT only.
            username: Optional filter to only return memories for a specific user.
            type: Optional filter by memory type (experience, technical, procedural, goal, individual).
            tags: Optional list of tags to filter by (returns memories matching any tag).
            importance_min: Optional minimum importance rating (1-10).
            importance_max: Optional maximum importance rating (1-10).
            created_after: Optional ISO datetime string to filter memories created after this date.
            created_before: Optional ISO datetime string to filter memories created before this date.
            memory_ids: Optional list of specific memory UUIDs to retrieve.
            offset: Pagination offset (default 0).
            limit: Maximum number of results to return (default 5, max 50).

        Returns:
            JSON string with search results including:
            - memories: List of matching memories (content truncated to 1000 chars)
            - total_count: Total number of matching memories
            - offset: Current pagination offset
            - limit: Results per page
            - has_more: Whether more results are available
        """
        try:
            # Parse and validate input
            memory_type = MemoryType(type) if type else None
            parsed_created_after = datetime.fromisoformat(created_after) if created_after else None
            parsed_created_before = datetime.fromisoformat(created_before) if created_before else None
            parsed_memory_ids = [UUID(uid) for uid in memory_ids] if memory_ids else None
            
            search_input = SearchMemoriesInput(
                query=query,
                username=username,
                type=memory_type,
                tags=tags,
                importance_min=importance_min,
                importance_max=importance_max,
                created_after=parsed_created_after,
                created_before=parsed_created_before,
                memory_ids=parsed_memory_ids,
                offset=offset,
                limit=min(limit, 50),
            )
            
            repo = await _get_repository()
            
            result = await repo.search(
                query=search_input.query,
                username=search_input.username,
                type=search_input.type.value if search_input.type else None,
                tags=search_input.tags,
                importance_min=search_input.importance_min,
                importance_max=search_input.importance_max,
                created_after=search_input.created_after,
                created_before=search_input.created_before,
                memory_ids=search_input.memory_ids,
                offset=search_input.offset,
                limit=search_input.limit,
            )
            
            # Serialize the results
            serialized = {
                "memories": [_serialize_truncated_memory(m) for m in result["memories"]],
                "total_count": result["total_count"],
                "offset": result["offset"],
                "limit": result["limit"],
                "has_more": result["has_more"],
            }
            
            return json.dumps(serialized, indent=2)
            
        except ValueError as e:
            return json.dumps({"error": f"Invalid input: {str(e)}"})
        except Exception as e:
            return json.dumps({"error": f"Search failed: {str(e)}"})

    @mcp.tool()
    async def search_memories_full(
        query: str,
        username: str | None = None,
        type: str | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        offset: int = 0,
        limit: int = 5,
    ) -> str:
        """Search across ALL text fields: content, tags, and metadata.

        Use this when you want to find memories where the search term might appear
        anywhere - in the content, tags, or metadata fields. This is broader than
        search_memories which only searches the content field.

        Args:
            query: Search query to match against content, tags, and metadata (required).
            username: Optional filter to only return memories for a specific user.
            type: Optional filter by memory type (experience, technical, procedural, goal, individual).
            importance_min: Optional minimum importance rating (1-10).
            importance_max: Optional maximum importance rating (1-10).
            offset: Pagination offset (default 0).
            limit: Maximum number of results to return (default 5, max 50).

        Returns:
            JSON string with search results including:
            - memories: List of matching memories (content truncated to 1000 chars)
            - total_count: Total number of matching memories
            - offset: Current pagination offset
            - limit: Results per page
            - has_more: Whether more results are available
        """
        try:
            if not query or not query.strip():
                return json.dumps({"error": "Query is required for full search"})
            
            memory_type = MemoryType(type) if type else None
            
            repo = await _get_repository()
            
            result = await repo.search_full(
                query=query.strip(),
                username=username,
                type=memory_type.value if memory_type else None,
                importance_min=importance_min,
                importance_max=importance_max,
                offset=offset,
                limit=min(limit, 50),
            )
            
            serialized = {
                "memories": [_serialize_truncated_memory(m) for m in result["memories"]],
                "total_count": result["total_count"],
                "offset": result["offset"],
                "limit": result["limit"],
                "has_more": result["has_more"],
            }
            
            return json.dumps(serialized, indent=2)
            
        except ValueError as e:
            return json.dumps({"error": f"Invalid input: {str(e)}"})
        except Exception as e:
            return json.dumps({"error": f"Full search failed: {str(e)}"})

    @mcp.tool()
    async def update_memory(
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        related_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Update an existing memory.

        Args:
            memory_id: The UUID of the memory to update.
            content: Optional new content for the memory.
            tags: Optional new list of tags (replaces existing tags).
            importance: Optional new importance rating (1-10).
            related_memory_ids: Optional new list of related memory UUIDs (replaces existing).
            metadata: Optional new metadata (replaces existing metadata).

        Returns:
            JSON string with the updated memory, or an error if not found.
        """
        try:
            uuid_id = UUID(memory_id)
            
            update_input = UpdateMemoryInput(
                content=content,
                tags=tags,
                importance=importance,
                related_memory_ids=[UUID(uid) for uid in related_memory_ids] if related_memory_ids else None,
                metadata=metadata,
            )
            
            repo = await _get_repository()
            
            result = await repo.update(
                memory_id=uuid_id,
                content=update_input.content,
                tags=update_input.tags,
                importance=update_input.importance,
                related_memory_ids=update_input.related_memory_ids,
                metadata=update_input.metadata,
            )
            
            if result is None:
                return json.dumps({"error": f"Memory not found: {memory_id}"})
            
            return json.dumps(_serialize_memory(result), indent=2)
            
        except ValueError as e:
            return json.dumps({"error": f"Invalid input: {str(e)}"})
        except Exception as e:
            return json.dumps({"error": f"Failed to update memory: {str(e)}"})

    @mcp.tool()
    async def delete_memory(memory_id: str) -> str:
        """Delete a memory (soft delete - can be recovered).

        Args:
            memory_id: The UUID of the memory to delete.

        Returns:
            JSON string indicating success or failure.
        """
        try:
            uuid_id = UUID(memory_id)
            
            repo = await _get_repository()
            
            success = await repo.delete(uuid_id)
            
            if not success:
                return json.dumps({"error": f"Memory not found: {memory_id}"})
            
            return json.dumps({
                "success": True,
                "message": f"Memory {memory_id} has been deleted",
            })
            
        except ValueError as e:
            return json.dumps({"error": f"Invalid memory ID format: {str(e)}"})
        except Exception as e:
            return json.dumps({"error": f"Failed to delete memory: {str(e)}"})

    @mcp.tool()
    async def get_existing_tags(
        username: str | None = None,
        type: str | None = None,
        limit: int = 50,
    ) -> str:
        """Get existing tags in the memory system with usage counts.

        Use this tool before creating memories to see what tags already exist,
        promoting consistency and reuse of existing tags.

        Args:
            username: Optional filter to only show tags used by a specific user.
            type: Optional filter by memory type (experience, technical, procedural, goal, individual).
            limit: Maximum number of tags to return (default 50, max 100).

        Returns:
            JSON string with list of {tag, count} sorted by usage count descending.
        """
        try:
            repo = await _get_repository()
            
            result = await repo.get_existing_tags(
                username=username,
                type=type,
                limit=min(limit, 100),
            )
            
            return json.dumps({
                "tags": result,
                "total_returned": len(result),
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": f"Failed to get tags: {str(e)}"})

    @mcp.tool()
    async def get_tag_suggestions(
        query: str,
        username: str | None = None,
        limit: int = 10,
    ) -> str:
        """Get tag suggestions based on fuzzy matching against existing tags.

        Use this tool when you have a tag in mind but want to check if a similar
        tag already exists to promote consistency.

        Args:
            query: The tag text to search for (partial matches supported).
            username: Optional filter to only search tags used by a specific user.
            limit: Maximum number of suggestions (default 10, max 25).

        Returns:
            JSON string with list of {tag, count, similarity} sorted by similarity descending.
        """
        try:
            if not query or not query.strip():
                return json.dumps({"error": "Query is required"})
            
            repo = await _get_repository()
            
            result = await repo.get_tag_suggestions(
                query=query.strip(),
                username=username,
                limit=min(limit, 25),
            )
            
            return json.dumps({
                "suggestions": result,
                "query": query.strip(),
                "total_returned": len(result),
            }, indent=2)
            
        except Exception as e:
            return json.dumps({"error": f"Failed to get tag suggestions: {str(e)}"})


def _serialize_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """Serialize a memory dict for JSON output."""
    return {
        "id": str(memory["id"]),
        "username": memory["username"],
        "type": memory["type"],
        "content": memory["content"],
        "tags": memory["tags"],
        "importance": memory["importance"],
        "related_memory_ids": [str(uid) for uid in memory["related_memory_ids"]],
        "metadata": memory["metadata"],
        "created_at": memory["created_at"].isoformat() if memory["created_at"] else None,
        "updated_at": memory["updated_at"].isoformat() if memory["updated_at"] else None,
        "deleted_at": memory["deleted_at"].isoformat() if memory.get("deleted_at") else None,
        "user_id": str(memory["user_id"]) if memory.get("user_id") else None,
    }


def _serialize_truncated_memory(memory: dict[str, Any]) -> dict[str, Any]:
    """Serialize a truncated memory dict for JSON output."""
    return {
        "id": str(memory["id"]),
        "username": memory["username"],
        "type": memory["type"],
        "content": memory["content"],
        "content_truncated": memory["content_truncated"],
        "tags": memory["tags"],
        "importance": memory["importance"],
        "related_memory_ids": [str(uid) for uid in memory["related_memory_ids"]],
        "created_at": memory["created_at"].isoformat() if memory["created_at"] else None,
        "updated_at": memory["updated_at"].isoformat() if memory["updated_at"] else None,
        "similarity_score": memory.get("similarity_score"),
        "user_id": str(memory["user_id"]) if memory.get("user_id") else None,
    }
