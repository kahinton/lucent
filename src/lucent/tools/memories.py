"""MCP tools for memory CRUD operations."""

import json
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from lucent.auth import get_current_api_key_id, get_current_user
from lucent.db import (
    AccessRepository,
    AuditRepository,
    MemoryRepository,
    VersionConflictError,
    get_pool,
    init_db,
)
from lucent.logging import get_logger
from lucent.mode import is_team_mode
from lucent.models.memory import (
    CreateMemoryInput,
    MemoryType,
    SearchMemoriesInput,
    UpdateMemoryInput,
)
from lucent.models.validation import validate_metadata

logger = get_logger("tools.memories")


def _error_response(message: str) -> str:
    """Create a consistent JSON error response for MCP tools.
    
    Args:
        message: The error message to include.
        
    Returns:
        JSON string with {"error": message} format.
    """
    return json.dumps({"error": message})


async def _get_current_user_id() -> UUID | None:
    """Get the current user ID from auth context."""
    current_user = get_current_user()
    if current_user:
        return current_user["id"]
    return None


async def _get_current_user_context() -> tuple[UUID | None, UUID | None, str | None]:
    """Get the current user ID, organization ID, and role.
    
    Returns:
        Tuple of (user_id, organization_id, role), any may be None.
    """
    current_user = get_current_user()
    if current_user:
        return current_user["id"], current_user.get("organization_id"), current_user.get("role", "member")
    return None, None, None


def _get_current_username() -> str | None:
    """Get the current user's display name or username.
    
    Returns:
        The user's display_name, email, username, or None if not authenticated.
    """
    current_user = get_current_user()
    if current_user:
        return current_user.get("display_name") or current_user.get("email") or current_user.get("username") or str(current_user["id"])
    return None


def _get_audit_context() -> dict[str, Any]:
    """Get the audit context including API key ID if authenticated via API key.
    
    Returns:
        Dict with auth_method and optional api_key_id.
    """
    api_key_id = get_current_api_key_id()
    if api_key_id:
        return {
            "auth_method": "api_key",
            "api_key_id": str(api_key_id),
        }
    return {"auth_method": "session"}


def _build_snapshot(memory: dict[str, Any]) -> dict[str, Any]:
    """Build a full snapshot of a memory's state for versioning.
    
    Captures all mutable fields so the memory can be restored to this exact state.
    
    Args:
        memory: The memory dict to snapshot.
        
    Returns:
        A JSON-serializable snapshot dict.
    """
    return {
        "content": memory["content"],
        "tags": memory["tags"],
        "importance": memory["importance"],
        "metadata": memory["metadata"],
        "related_memory_ids": [str(uid) for uid in memory.get("related_memory_ids", [])],
        "shared": memory.get("shared", False),
    }


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


async def _get_audit_repository() -> AuditRepository:
    """Get an audit repository, initializing the database if needed."""
    try:
        pool = await get_pool()
    except RuntimeError:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        pool = await init_db(database_url)
    return AuditRepository(pool)


async def _get_access_repository() -> AccessRepository:
    """Get an access repository, initializing the database if needed."""
    try:
        pool = await get_pool()
    except RuntimeError:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is required")
        pool = await init_db(database_url)
    return AccessRepository(pool)


def register_tools(mcp: FastMCP) -> None:
    """Register all memory tools with the MCP server."""

    # Build the docstring dynamically from the models
    # Note: Individual memories are excluded because they are auto-created when users join
    create_memory_description = """Create a new memory in the knowledge base.

Args:
    type: Type of memory - one of: experience, technical, procedural, goal.
    content: The main content/description of the memory.
    username: Optional username (defaults to authenticated user).
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

    @mcp.tool(description=create_memory_description)
    async def create_memory(
        type: str,
        content: str,
        username: str | None = None,
        tags: list[str] | None = None,
        importance: int = 5,
        related_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        try:
            # Validate input
            memory_type = MemoryType(type)

            # Individual memories cannot be created via MCP - they are auto-created when users are added
            if memory_type == MemoryType.INDIVIDUAL:
                return _error_response(
                    "Individual memories cannot be created directly. They are automatically created when users are added to the system."
                )

            # Validate and normalize metadata for the memory type
            validated_metadata = validate_metadata(memory_type, metadata)

            # Use authenticated user's name if username not provided
            effective_username = username or _get_current_username() or "unknown"

            logger.info("create_memory: type=%s, user=%s, tags=%s", type, effective_username, tags)

            input_data = CreateMemoryInput(
                username=effective_username,
                type=memory_type,
                content=content,
                tags=tags or [],
                importance=importance,
                related_memory_ids=[UUID(uid) for uid in (related_memory_ids or [])],
                metadata=validated_metadata,
            )

            # Get current user context (from auth context or dev mode)
            user_id, org_id, user_role = await _get_current_user_context()

            repo = await _get_repository()

            result = await repo.create(
                username=effective_username,
                type=input_data.type.value,
                content=input_data.content,
                tags=input_data.tags,
                importance=input_data.importance,
                related_memory_ids=input_data.related_memory_ids,
                metadata=input_data.metadata,
                user_id=user_id,
                organization_id=org_id,
            )

            # Log the creation in audit log with version snapshot
            audit_repo = await _get_audit_repository()
            await audit_repo.log(
                memory_id=result["id"],
                action_type="create",
                user_id=user_id,
                organization_id=org_id,
                new_values={
                    "username": input_data.username,
                    "type": input_data.type.value,
                    "content": input_data.content,
                    "tags": input_data.tags,
                    "importance": input_data.importance,
                    "metadata": input_data.metadata,
                },
                context=_get_audit_context(),
                version=1,
                snapshot=_build_snapshot(result),
            )

            return json.dumps(_serialize_memory(result), indent=2)

        except ValueError as e:
            logger.warning("create_memory validation error: %s", e)
            return _error_response(str(e))
        except Exception as e:
            logger.error("create_memory failed", exc_info=e)
            return _error_response(f"Failed to create memory: {e}")

    @mcp.tool()
    async def get_memory(memory_id: str) -> str:
        """Retrieve a memory by its ID.

        Returns the memory only if you own it or it's shared within your organization.
        
        For retrieving multiple memories at once, use get_memories instead.

        Args:
            memory_id: The UUID of the memory to retrieve.

        Returns:
            JSON string with the full memory details, or an error if not found or not accessible.
        """
        try:
            uuid_id = UUID(memory_id)

            logger.debug("get_memory: id=%s", memory_id)

            repo = await _get_repository()

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

            if user_id is not None and org_id is not None:
                # Use access-controlled get
                result = await repo.get_accessible(uuid_id, user_id, org_id)
            else:
                # No auth context, use basic get (for backward compatibility)
                result = await repo.get(uuid_id)

            if result is None:
                return _error_response(f"Memory not found or not accessible: {memory_id}")

            # Log the access (team mode only)
            if is_team_mode():
                try:
                    access_repo = await _get_access_repository()
                    await access_repo.log_access(
                        memory_id=uuid_id,
                        access_type="view",
                        user_id=user_id,
                        organization_id=org_id,
                    )
                except Exception:
                    pass  # Don't fail the request if access logging fails

            return json.dumps(_serialize_memory(result), indent=2)

        except ValueError as e:
            return _error_response(f"Invalid memory ID format: {e}")
        except Exception as e:
            logger.error("get_memory failed: id=%s", memory_id, exc_info=e)
            return _error_response(f"Failed to retrieve memory: {e}")

    @mcp.tool()
    async def get_memories(memory_ids: list[str]) -> str:
        """Retrieve multiple memories by their IDs in a single call.

        Returns only memories you own or that are shared within your organization.
        More efficient than calling get_memory multiple times when you need
        several memories (e.g., after a search returns truncated results).

        Args:
            memory_ids: List of UUIDs of memories to retrieve.

        Returns:
            JSON string with:
            - memories: List of full memory details for accessible memories
            - not_found: List of IDs that were not found or not accessible
            - total_requested: Number of IDs requested
            - total_found: Number of memories successfully retrieved
        """
        try:
            if not memory_ids:
                return _error_response("memory_ids list cannot be empty")

            # Parse and validate all UUIDs first
            try:
                uuid_ids = [UUID(mid) for mid in memory_ids]
            except ValueError as e:
                return _error_response(f"Invalid memory ID format: {e}")

            repo = await _get_repository()
            access_repo = await _get_access_repository()

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

            memories = []
            not_found = []

            for uuid_id, original_id in zip(uuid_ids, memory_ids):
                if user_id is not None and org_id is not None:
                    result = await repo.get_accessible(uuid_id, user_id, org_id)
                else:
                    result = await repo.get(uuid_id)

                if result is None:
                    not_found.append(original_id)
                else:
                    memories.append(_serialize_memory(result))
                    # Log access (team mode only)
                    if is_team_mode():
                        try:
                            await access_repo.log_access(
                                memory_id=uuid_id,
                                access_type="view",
                                user_id=user_id,
                                organization_id=org_id,
                            )
                        except Exception:
                            pass

            return json.dumps({
                "memories": memories,
                "not_found": not_found,
                "total_requested": len(memory_ids),
                "total_found": len(memories),
            }, indent=2)

        except Exception as e:
            return _error_response(f"Failed to retrieve memories: {str(e)}")

    @mcp.tool()
    async def get_current_user_context() -> str:
        """Get the current authenticated user's context and their individual memory.

        This is the recommended way to start a conversation - call this first to get:
        - Who you're talking to (name, email, role)
        - Their individual memory with preferences, working style, and history
        - Recent project context they've been working on

        Returns:
            JSON string with:
            - user: Basic user info (id, name, email, role)
            - individual_memory: Their full individual memory if it exists
            - error: Error message if not authenticated
        """
        try:
            user_id, org_id, user_role = await _get_current_user_context()

            if user_id is None:
                return _error_response("Not authenticated")

            current_user = get_current_user()

            # Build user info
            user_info = {
                "id": str(user_id),
                "organization_id": str(org_id) if org_id else None,
                "role": user_role,
            }

            if current_user:
                user_info["display_name"] = current_user.get("display_name")
                user_info["email"] = current_user.get("email")

            # Get their individual memory
            repo = await _get_repository()
            individual_memory = await repo.get_individual_memory_for_user(user_id)

            result = {
                "user": user_info,
                "individual_memory": _serialize_memory(individual_memory) if individual_memory else None,
            }

            return json.dumps(result, indent=2)

        except Exception as e:
            return _error_response(f"Failed to get user context: {str(e)}")

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
            tags: Optional list of tags to filter by (memories must have all specified tags).
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

            logger.info("search_memories: query=%s, type=%s, tags=%s", query, type, tags)

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

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

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
                requesting_user_id=user_id,
                requesting_org_id=org_id,
            )

            # Log access for returned memories (team mode only)
            if result["memories"] and is_team_mode():
                try:
                    access_repo = await _get_access_repository()
                    memory_ids_accessed = [m["id"] for m in result["memories"]]
                    await access_repo.log_batch_access(
                        memory_ids=memory_ids_accessed,
                        access_type="search_result",
                        user_id=user_id,
                        organization_id=org_id,
                        context={
                            "query": search_input.query,
                            "type": search_input.type.value if search_input.type else None,
                            "tags": search_input.tags,
                        },
                    )
                except Exception:
                    pass  # Don't fail the request if access logging fails

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
            return _error_response(f"Invalid input: {str(e)}")
        except Exception as e:
            logger.error("search_memories failed", exc_info=e)
            return _error_response(f"Search failed: {str(e)}")

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
                return _error_response("Query is required for full search")

            logger.info("search_memories_full: query=%s, type=%s", query, type)

            memory_type = MemoryType(type) if type else None

            repo = await _get_repository()

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

            result = await repo.search_full(
                query=query.strip(),
                username=username,
                type=memory_type.value if memory_type else None,
                importance_min=importance_min,
                importance_max=importance_max,
                offset=offset,
                limit=min(limit, 50),
                requesting_user_id=user_id,
                requesting_org_id=org_id,
            )

            # Log access for returned memories (team mode only)
            if result["memories"] and is_team_mode():
                try:
                    access_repo = await _get_access_repository()
                    memory_ids_accessed = [m["id"] for m in result["memories"]]
                    await access_repo.log_batch_access(
                        memory_ids=memory_ids_accessed,
                        access_type="search_result",
                        user_id=user_id,
                        organization_id=org_id,
                        context={
                            "query": query.strip(),
                            "search_type": "full",
                            "type": memory_type.value if memory_type else None,
                        },
                    )
                except Exception:
                    pass  # Don't fail the request if access logging fails

            serialized = {
                "memories": [_serialize_truncated_memory(m) for m in result["memories"]],
                "total_count": result["total_count"],
                "offset": result["offset"],
                "limit": result["limit"],
                "has_more": result["has_more"],
            }

            return json.dumps(serialized, indent=2)

        except ValueError as e:
            return _error_response(f"Invalid input: {str(e)}")
        except Exception as e:
            logger.error("search_memories_full failed", exc_info=e)
            return _error_response(f"Full search failed: {str(e)}")

    @mcp.tool()
    async def update_memory(
        memory_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        related_memory_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> str:
        """Update an existing memory.

        Args:
            memory_id: The UUID of the memory to update.
            content: Optional new content for the memory.
            tags: Optional new list of tags (replaces existing tags).
            importance: Optional new importance rating (1-10).
            related_memory_ids: Optional new list of related memory UUIDs (replaces existing).
            metadata: Optional new metadata (replaces existing). Must match the memory's type schema.
                      See create_memory for the metadata schema for each memory type.
            expected_version: Optional optimistic lock. If provided, the update only succeeds
                if the memory's current version matches this value. Use this to prevent
                concurrent updates from overwriting each other. Get the current version
                from the memory's "version" field.

        Returns:
            JSON string with the updated memory, or an error if not found or version conflict.
        """
        try:
            uuid_id = UUID(memory_id)

            logger.info("update_memory: id=%s", memory_id)

            repo = await _get_repository()

            # Get current user context for ownership check
            user_id, org_id, user_role = await _get_current_user_context()

            if user_id is None:
                return _error_response("Authentication required to update memories")

            # Get old values before update for audit (also needed for metadata validation)
            # Use get_accessible to ensure user can at least see this memory
            old_memory = await repo.get_accessible(uuid_id, user_id, org_id)
            if old_memory is None:
                return _error_response(f"Memory not found or not accessible: {memory_id}")

            # Check ownership - only the owner can update a memory
            if old_memory.get("user_id") != user_id:
                return _error_response("Permission denied: only the owner can update this memory")

            # Validate metadata if provided
            validated_metadata = metadata
            if metadata is not None:
                validated_metadata = validate_metadata(old_memory["type"], metadata)

            update_input = UpdateMemoryInput(
                content=content,
                tags=tags,
                importance=importance,
                related_memory_ids=[UUID(uid) for uid in related_memory_ids] if related_memory_ids else None,
                metadata=validated_metadata,
            )

            result = await repo.update(
                memory_id=uuid_id,
                content=update_input.content,
                tags=update_input.tags,
                importance=update_input.importance,
                related_memory_ids=update_input.related_memory_ids,
                metadata=update_input.metadata,
                expected_version=expected_version,
            )

            if result is None:
                return _error_response(f"Memory not found: {memory_id}")

            # Build audit log entry
            changed_fields = []
            old_values = {}
            new_values = {}

            if content is not None and old_memory["content"] != content:
                changed_fields.append("content")
                old_values["content"] = old_memory["content"]
                new_values["content"] = content

            if tags is not None and old_memory["tags"] != tags:
                changed_fields.append("tags")
                old_values["tags"] = old_memory["tags"]
                new_values["tags"] = tags

            if importance is not None and old_memory["importance"] != importance:
                changed_fields.append("importance")
                old_values["importance"] = old_memory["importance"]
                new_values["importance"] = importance

            if metadata is not None and old_memory["metadata"] != metadata:
                changed_fields.append("metadata")
                old_values["metadata"] = old_memory["metadata"]
                new_values["metadata"] = metadata

            if related_memory_ids is not None:
                old_related = [str(uid) for uid in old_memory["related_memory_ids"]]
                if old_related != related_memory_ids:
                    changed_fields.append("related_memory_ids")
                    old_values["related_memory_ids"] = old_related
                    new_values["related_memory_ids"] = related_memory_ids

            # Log the update with version snapshot
            if changed_fields:
                audit_repo = await _get_audit_repository()
                await audit_repo.log(
                    memory_id=uuid_id,
                    action_type="update",
                    user_id=user_id,
                    organization_id=org_id,
                    changed_fields=changed_fields,
                    old_values=old_values,
                    new_values=new_values,
                    context=_get_audit_context(),
                    version=result["version"],
                    snapshot=_build_snapshot(result),
                )

            return json.dumps(_serialize_memory(result), indent=2)

        except VersionConflictError as e:
            logger.warning("update_memory version conflict: id=%s, expected=%s, actual=%s",
                           memory_id, e.expected_version, e.actual_version)
            return _error_response(
                f"Version conflict: memory was modified by another process. "
                f"Expected version {e.expected_version}, current version {e.actual_version}. "
                f"Re-read the memory and retry."
            )
        except ValueError as e:
            return _error_response(f"Invalid input: {str(e)}")
        except Exception as e:
            logger.error("update_memory failed: id=%s", memory_id, exc_info=e)
            return _error_response(f"Failed to update memory: {str(e)}")

    @mcp.tool()
    async def delete_memory(memory_id: str) -> str:
        """Delete a memory (soft delete - can be recovered).

        NOTE: Individual memories cannot be deleted via this tool - they are
        automatically deleted when users are removed from the system.

        Args:
            memory_id: The UUID of the memory to delete.

        Returns:
            JSON string indicating success or failure.
        """
        try:
            uuid_id = UUID(memory_id)

            logger.info("delete_memory: id=%s", memory_id)

            repo = await _get_repository()

            # Get current user context for ownership check
            user_id, org_id, user_role = await _get_current_user_context()

            if user_id is None:
                return _error_response("Authentication required to delete memories")

            # Get memory info before deletion for audit
            # Use get_accessible to ensure user can at least see this memory
            old_memory = await repo.get_accessible(uuid_id, user_id, org_id)
            if old_memory is None:
                return _error_response(f"Memory not found or not accessible: {memory_id}")

            # Check ownership - only the owner can delete a memory
            if old_memory.get("user_id") != user_id:
                return _error_response("Permission denied: only the owner can delete this memory")

            # Individual memories cannot be deleted via MCP - they are deleted when users are removed
            if old_memory.get("type") == "individual":
                return _error_response(
                    "Individual memories cannot be deleted directly. They are automatically deleted when users are removed from the system."
                )

            success = await repo.delete(uuid_id)

            if not success:
                return _error_response(f"Memory not found: {memory_id}")

            # Log the deletion with final snapshot
            audit_repo = await _get_audit_repository()
            await audit_repo.log(
                memory_id=uuid_id,
                action_type="delete",
                user_id=user_id,
                organization_id=org_id,
                old_values={
                    "content": old_memory["content"],
                    "tags": old_memory["tags"],
                    "importance": old_memory["importance"],
                },
                context=_get_audit_context(),
                snapshot=_build_snapshot(old_memory),
            )

            return json.dumps({
                "success": True,
                "message": f"Memory {memory_id} has been deleted",
            })

        except ValueError as e:
            return _error_response(f"Invalid memory ID format: {str(e)}")
        except Exception as e:
            logger.error("delete_memory failed: id=%s", memory_id, exc_info=e)
            return _error_response(f"Failed to delete memory: {str(e)}")

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

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

            result = await repo.get_existing_tags(
                username=username,
                type=type,
                limit=min(limit, 100),
                requesting_user_id=user_id,
                requesting_org_id=org_id,
            )

            return json.dumps({
                "tags": result,
                "total_returned": len(result),
            }, indent=2)

        except Exception as e:
            return _error_response(f"Failed to get tags: {str(e)}")

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
                return _error_response("Query is required")

            repo = await _get_repository()

            # Get current user context for access control
            user_id, org_id, user_role = await _get_current_user_context()

            result = await repo.get_tag_suggestions(
                query=query.strip(),
                username=username,
                limit=min(limit, 25),
                requesting_user_id=user_id,
                requesting_org_id=org_id,
            )

            return json.dumps({
                "suggestions": result,
                "query": query.strip(),
                "total_returned": len(result),
            }, indent=2)

        except Exception as e:
            return _error_response(f"Failed to get tag suggestions: {str(e)}")

    @mcp.tool()
    async def get_memory_versions(
        memory_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Get the version history for a memory.

        Returns a list of all versions with timestamps, who made each change,
        what fields were changed, and whether a restorable snapshot exists.
        Use this to review the history of changes to a memory or to find
        a specific version to restore.

        Args:
            memory_id: The UUID of the memory to get versions for.
            limit: Maximum number of versions to return (default 20, max 50).
            offset: Pagination offset (default 0).

        Returns:
            JSON string with:
            - versions: List of version entries with version number, action, timestamp, changes
            - current_version: The memory's current version number
            - total_count: Total number of versions
            - has_more: Whether more versions are available
        """
        try:
            uuid_id = UUID(memory_id)
            repo = await _get_repository()
            audit_repo = await _get_audit_repository()

            # Verify the user can access this memory
            user_id, org_id, user_role = await _get_current_user_context()
            if user_id is None:
                return _error_response("Authentication required")

            memory = await repo.get_accessible(uuid_id, user_id, org_id)
            if memory is None:
                return _error_response(f"Memory not found or not accessible: {memory_id}")

            result = await audit_repo.get_versions(
                memory_id=uuid_id,
                limit=min(limit, 50),
                offset=offset,
            )

            versions = []
            for entry in result["versions"]:
                versions.append({
                    "version": entry["version"],
                    "action_type": entry["action_type"],
                    "created_at": entry["created_at"].isoformat() if entry["created_at"] else None,
                    "changed_fields": entry["changed_fields"],
                    "has_snapshot": entry.get("snapshot") is not None,
                    "user_id": str(entry["user_id"]) if entry.get("user_id") else None,
                    "notes": entry.get("notes"),
                })

            return json.dumps({
                "memory_id": memory_id,
                "current_version": memory["version"],
                "versions": versions,
                "total_count": result["total_count"],
                "offset": result["offset"],
                "limit": result["limit"],
                "has_more": result["has_more"],
            }, indent=2)

        except ValueError as e:
            return _error_response(f"Invalid memory ID format: {e}")
        except Exception as e:
            return _error_response(f"Failed to get memory versions: {e}")

    @mcp.tool()
    async def restore_memory_version(
        memory_id: str,
        version: int,
    ) -> str:
        """Restore a memory to a previous version.

        This creates a new version with the content from the specified historical
        version. The restore is logged as a new audit entry so version history
        is never lost.

        Only the owner of the memory can restore versions.

        Args:
            memory_id: The UUID of the memory to restore.
            version: The version number to restore to.

        Returns:
            JSON string with the restored memory, or an error if the version
            doesn't exist or doesn't have a snapshot.
        """
        try:
            uuid_id = UUID(memory_id)
            repo = await _get_repository()
            audit_repo = await _get_audit_repository()

            # Verify ownership
            user_id, org_id, user_role = await _get_current_user_context()
            if user_id is None:
                return _error_response("Authentication required to restore memories")

            memory = await repo.get_accessible(uuid_id, user_id, org_id)
            if memory is None:
                return _error_response(f"Memory not found or not accessible: {memory_id}")

            if memory.get("user_id") != user_id:
                return _error_response("Permission denied: only the owner can restore versions")

            if memory["version"] == version:
                return _error_response(f"Memory is already at version {version}")

            # Get the target version's snapshot
            version_entry = await audit_repo.get_version_snapshot(uuid_id, version)
            if version_entry is None:
                return _error_response(f"Version {version} not found for this memory")

            snapshot = version_entry.get("snapshot")
            if snapshot is None:
                return _error_response(
                    f"Version {version} does not have a restorable snapshot. "
                    "Snapshots are only available for versions created after versioning was enabled."
                )

            # Apply the snapshot
            result = await repo.update(
                memory_id=uuid_id,
                content=snapshot.get("content"),
                tags=snapshot.get("tags"),
                importance=snapshot.get("importance"),
                metadata=snapshot.get("metadata"),
                related_memory_ids=[UUID(uid) for uid in snapshot.get("related_memory_ids", [])],
            )

            if result is None:
                return _error_response("Failed to apply restore")

            # Log the restore as a new version
            await audit_repo.log(
                memory_id=uuid_id,
                action_type="restore",
                user_id=user_id,
                organization_id=org_id,
                old_values=_build_snapshot(memory),
                new_values=snapshot,
                context=_get_audit_context(),
                notes=f"Restored to version {version}",
                version=result["version"],
                snapshot=_build_snapshot(result),
            )

            return json.dumps({
                **_serialize_memory(result),
                "restored_from_version": version,
                "message": f"Memory restored to version {version} (now at version {result['version']})",
            }, indent=2)

        except ValueError as e:
            return _error_response(f"Invalid input: {e}")
        except Exception as e:
            return _error_response(f"Failed to restore memory version: {e}")

    # Team-only tools: sharing
    if is_team_mode():
        @mcp.tool()
        async def share_memory(memory_id: str) -> str:
            """Share a memory with other users in your organization.

            Only the owner of the memory can share it. Once shared, other users
            in the same organization will be able to see this memory in their
            search results.

            Args:
                memory_id: The UUID of the memory to share.

            Returns:
                JSON string with the updated memory showing shared=true, or an error.
            """
            try:
                # Get current user context
                user_id, org_id, user_role = await _get_current_user_context()

                if user_id is None:
                    return _error_response("Authentication required to share memories")

                repo = await _get_repository()

                result = await repo.set_shared(
                    memory_id=UUID(memory_id),
                    user_id=user_id,
                    shared=True,
                )

                if result is None:
                    return _error_response(
                        "Memory not found or you are not the owner. Only the owner can share a memory."
                    )

                # Log the share action
                audit_repo = await _get_audit_repository()
                await audit_repo.log(
                    memory_id=UUID(memory_id),
                    action_type="share",
                    user_id=user_id,
                    organization_id=org_id,
                    changed_fields=["shared"],
                    old_values={"shared": False},
                    new_values={"shared": True},
                    context=_get_audit_context(),
                )

                return json.dumps(_serialize_memory(result), indent=2)

            except ValueError as e:
                return _error_response(f"Invalid memory_id: {str(e)}")
            except Exception as e:
                return _error_response(f"Failed to share memory: {str(e)}")

        @mcp.tool()
        async def unshare_memory(memory_id: str) -> str:
            """Stop sharing a memory with your organization.

            Only the owner of the memory can unshare it. Once unshared, the memory
            will only be visible to the owner.

            Args:
                memory_id: The UUID of the memory to unshare.

            Returns:
                JSON string with the updated memory showing shared=false, or an error.
            """
            try:
                # Get current user context
                user_id, org_id, user_role = await _get_current_user_context()

                if user_id is None:
                    return _error_response("Authentication required to unshare memories")

                repo = await _get_repository()

                result = await repo.set_shared(
                    memory_id=UUID(memory_id),
                    user_id=user_id,
                    shared=False,
                )

                if result is None:
                    return _error_response(
                        "Memory not found or you are not the owner. Only the owner can unshare a memory."
                    )

                # Log the unshare action
                audit_repo = await _get_audit_repository()
                await audit_repo.log(
                    memory_id=UUID(memory_id),
                    action_type="unshare",
                    user_id=user_id,
                    organization_id=org_id,
                    changed_fields=["shared"],
                    old_values={"shared": True},
                    new_values={"shared": False},
                    context=_get_audit_context(),
                )

                return json.dumps(_serialize_memory(result), indent=2)

            except ValueError as e:
                return _error_response(f"Invalid memory_id: {str(e)}")
            except Exception as e:
                return _error_response(f"Failed to unshare memory: {str(e)}")

    @mcp.tool()
    async def create_daemon_task(
        description: str,
        agent_type: str = "code",
        priority: str = "medium",
        context: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Create a new daemon task for autonomous processing.

        Submits a task that will be picked up by the daemon's cognitive cycle.
        The task is stored as a memory tagged with 'daemon-task' and 'pending'.

        Args:
            description: What the daemon should do. Be specific and actionable.
            agent_type: Type of agent to handle the task. One of: research, code,
                memory, reflection, documentation, planning. Default: code.
            priority: Task priority. One of: low, medium, high. Default: medium.
            context: Optional additional context or constraints for the agent.
            tags: Optional extra tags for categorization.

        Returns:
            JSON string with the created task including its ID and status.
        """
        valid_agent_types = {"research", "code", "memory", "reflection", "documentation", "planning"}
        valid_priorities = {"low", "medium", "high"}

        if agent_type not in valid_agent_types:
            return _error_response(
                f"Invalid agent_type '{agent_type}'. Must be one of: {', '.join(sorted(valid_agent_types))}"
            )
        if priority not in valid_priorities:
            return _error_response(
                f"Invalid priority '{priority}'. Must be one of: {', '.join(sorted(valid_priorities))}"
            )

        try:
            repo = await _get_repository()
            user_id, org_id, _ = await _get_current_user_context()
            if user_id is None:
                return _error_response("Authentication required")

            username = _get_current_username() or "unknown"

            # Build tags
            memory_tags = ["daemon-task", "daemon", "pending", agent_type, priority]
            if tags:
                memory_tags.extend(tags)

            # Build metadata
            metadata: dict[str, Any] = {"submitted_by": str(user_id), "source": "mcp"}
            if context:
                metadata["context"] = context

            result = await repo.create(
                username=username,
                type="technical",
                content=description,
                tags=memory_tags,
                importance={"low": 3, "medium": 5, "high": 8}.get(priority, 5),
                metadata=metadata,
                user_id=user_id,
                organization_id=org_id,
            )

            # Audit
            audit_repo = await _get_audit_repository()
            await audit_repo.log(
                memory_id=result["id"],
                action_type="create",
                user_id=user_id,
                organization_id=org_id,
                context={**_get_audit_context(), "action": "create_daemon_task"},
                version=result.get("version", 1),
                snapshot=_build_snapshot(result),
            )

            return json.dumps(_serialize_memory(result), indent=2)

        except Exception as e:
            return _error_response(f"Failed to create daemon task: {str(e)}")

    @mcp.tool()
    async def claim_task(
        memory_id: str,
        instance_id: str,
    ) -> str:
        """Atomically claim a pending daemon task for a specific instance.

        Uses database-level locking to prevent race conditions between daemon
        instances. Only succeeds if the memory has a 'pending' tag and no
        existing claim. On success, replaces 'pending' with
        'claimed-by-{instance_id}' in the tags.

        Args:
            memory_id: The UUID of the task memory to claim.
            instance_id: The unique identifier of the claiming daemon instance.

        Returns:
            JSON string with the claimed memory if successful, or an error if
            the task was already claimed or is not pending.
        """
        try:
            uuid_id = UUID(memory_id)
            repo = await _get_repository()

            user_id, org_id, _ = await _get_current_user_context()
            if user_id is None:
                return _error_response("Authentication required")

            result = await repo.claim_task(
                memory_id=uuid_id,
                instance_id=instance_id,
            )

            if result is None:
                return _error_response(
                    f"Could not claim task {memory_id}: "
                    "either not found, not pending, or already claimed by another instance."
                )

            # Audit the claim
            audit_repo = await _get_audit_repository()
            await audit_repo.log(
                memory_id=uuid_id,
                action_type="update",
                user_id=user_id,
                organization_id=org_id,
                changed_fields=["tags"],
                old_values={"tags": "pending"},
                new_values={"tags": f"claimed-by-{instance_id}"},
                context={**_get_audit_context(), "action": "claim_task", "instance_id": instance_id},
                version=result["version"],
                snapshot=_build_snapshot(result),
            )

            return json.dumps(_serialize_memory(result), indent=2)

        except ValueError as e:
            return _error_response(f"Invalid input: {str(e)}")
        except Exception as e:
            return _error_response(f"Failed to claim task: {str(e)}")

    @mcp.tool()
    async def release_claim(
        memory_id: str,
        instance_id: str | None = None,
    ) -> str:
        """Release a claimed daemon task back to pending state.

        If instance_id is provided, only releases the task if it was claimed by
        that specific instance. If not provided, releases any claim.

        Args:
            memory_id: The UUID of the task memory to release.
            instance_id: Optional — only release if claimed by this instance.

        Returns:
            JSON string with the released memory, or an error.
        """
        try:
            uuid_id = UUID(memory_id)
            repo = await _get_repository()

            user_id, org_id, _ = await _get_current_user_context()
            if user_id is None:
                return _error_response("Authentication required")

            result = await repo.release_claim(
                memory_id=uuid_id,
                instance_id=instance_id,
            )

            if result is None:
                return _error_response(
                    f"Could not release task {memory_id}: not found or not claimed"
                    + (f" by {instance_id}" if instance_id else "") + "."
                )

            # Audit the release
            audit_repo = await _get_audit_repository()
            await audit_repo.log(
                memory_id=uuid_id,
                action_type="update",
                user_id=user_id,
                organization_id=org_id,
                changed_fields=["tags"],
                old_values={"tags": f"claimed-by-{instance_id}" if instance_id else "claimed"},
                new_values={"tags": "pending"},
                context={**_get_audit_context(), "action": "release_claim", "instance_id": instance_id},
                version=result["version"],
                snapshot=_build_snapshot(result),
            )

            return json.dumps(_serialize_memory(result), indent=2)

        except ValueError as e:
            return _error_response(f"Invalid input: {str(e)}")
        except Exception as e:
            return _error_response(f"Failed to release claim: {str(e)}")

    @mcp.tool()
    async def export_memories(
        type: str | None = None,
        tags: list[str] | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> str:
        """Export memories with full, untruncated content.

        Returns all matching memories without content truncation or pagination,
        suitable for backup or migration. Access-controlled: only returns memories
        you own or that are shared within your organization.

        Args:
            type: Filter by memory type (experience, technical, procedural, goal, individual).
            tags: Filter by tags (returns memories matching any provided tag).
            importance_min: Minimum importance (1-10).
            importance_max: Maximum importance (1-10).
            created_after: ISO 8601 datetime string — only include memories created after this time.
            created_before: ISO 8601 datetime string — only include memories created before this time.

        Returns:
            JSON string with export metadata and full memory records.
        """
        try:
            user_id, org_id, user_role = await _get_current_user_context()

            parsed_after = datetime.fromisoformat(created_after) if created_after else None
            parsed_before = datetime.fromisoformat(created_before) if created_before else None

            repo = await _get_repository()
            memories = await repo.export(
                type=type,
                tags=tags,
                importance_min=importance_min,
                importance_max=importance_max,
                created_after=parsed_after,
                created_before=parsed_before,
                requesting_user_id=user_id,
                requesting_org_id=org_id,
            )

            serialized = [_serialize_memory(m) for m in memories]

            result = {
                "metadata": {
                    "exported_at": datetime.now().isoformat(),
                    "total_count": len(serialized),
                    "filters": {
                        k: v for k, v in {
                            "type": type,
                            "tags": tags,
                            "importance_min": importance_min,
                            "importance_max": importance_max,
                            "created_after": created_after,
                            "created_before": created_before,
                        }.items() if v is not None
                    },
                    "format": "json",
                },
                "memories": serialized,
            }
            return json.dumps(result, indent=2)

        except ValueError as e:
            return _error_response(f"Invalid parameter: {str(e)}")
        except Exception as e:
            return _error_response(f"Failed to export memories: {str(e)}")

    @mcp.tool()
    async def import_memories(
        memories_json: str,
    ) -> str:
        """Import memories from a previously exported JSON payload.

        Accepts a JSON string containing a list of memory objects (matching the
        format returned by export_memories). Deduplicates by content hash —
        memories with identical content, type, and username already in your
        account are skipped. All imported memories are owned by the
        authenticated user.

        Args:
            memories_json: JSON string — either a list of memory objects, or
                an export object with a "memories" key containing the list.

        Returns:
            JSON string with import summary: imported count, skipped count,
            errors, and total.
        """
        try:
            user_id, org_id, user_role = await _get_current_user_context()
            username = _get_current_username()

            # Parse input
            try:
                data = json.loads(memories_json)
            except json.JSONDecodeError as e:
                return _error_response(f"Invalid JSON: {str(e)}")

            # Accept either a raw list or an export object with "memories" key
            if isinstance(data, dict) and "memories" in data:
                memory_list = data["memories"]
            elif isinstance(data, list):
                memory_list = data
            else:
                return _error_response(
                    "Expected a JSON list of memories or an export object with a 'memories' key"
                )

            if not isinstance(memory_list, list):
                return _error_response("'memories' must be a list")

            repo = await _get_repository()
            result = await repo.import_memories(
                memories=memory_list,
                requesting_user_id=user_id,
                requesting_org_id=org_id,
                requesting_username=username,
            )

            return json.dumps(result, indent=2)

        except Exception as e:
            return _error_response(f"Failed to import memories: {str(e)}")


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
        "version": memory.get("version", 1),
        "created_at": memory["created_at"].isoformat() if memory["created_at"] else None,
        "updated_at": memory["updated_at"].isoformat() if memory["updated_at"] else None,
        "deleted_at": memory["deleted_at"].isoformat() if memory.get("deleted_at") else None,
        "user_id": str(memory["user_id"]) if memory.get("user_id") else None,
        "organization_id": str(memory["organization_id"]) if memory.get("organization_id") else None,
        "shared": memory.get("shared", False),
        "last_accessed_at": memory["last_accessed_at"].isoformat() if memory.get("last_accessed_at") else None,
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
        "organization_id": str(memory["organization_id"]) if memory.get("organization_id") else None,
        "shared": memory.get("shared", False),
        "last_accessed_at": memory["last_accessed_at"].isoformat() if memory.get("last_accessed_at") else None,
    }
