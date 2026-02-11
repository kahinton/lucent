"""Memory repository for Lucent.

Handles CRUD operations for memories including search functionality.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool


class MemoryRepository:
    """Repository for memory CRUD operations."""
    
    TRUNCATE_LENGTH = 1000
    
    def __init__(self, pool: Pool):
        self.pool = pool
    
    async def create(
        self,
        username: str,
        type: str,
        content: str,
        tags: list[str] | None = None,
        importance: int = 5,
        related_memory_ids: list[UUID] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: UUID | None = None,
        organization_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Create a new memory.
        
        Args:
            username: The username of the user creating the memory.
            type: The type of memory.
            content: The main content of the memory.
            tags: Optional list of tags.
            importance: Importance rating (1-10).
            related_memory_ids: Optional list of related memory UUIDs.
            metadata: Optional type-specific metadata.
            user_id: Optional user ID (foreign key to users table).
            organization_id: Optional organization ID (for efficient org-scoped queries).
            
        Returns:
            The created memory record.
        """
        # Validate related memory IDs exist and are not deleted
        if related_memory_ids:
            await self._validate_related_ids(related_memory_ids)
        
        query = """
            INSERT INTO memories (username, type, content, tags, importance, related_memory_ids, metadata, user_id, organization_id, shared)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, false)
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata, 
                      created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                username,
                type,
                content,
                tags or [],
                importance,
                [str(uid) for uid in (related_memory_ids or [])],
                metadata or {},
                str(user_id) if user_id else None,
                str(organization_id) if organization_id else None,
            )
        
        return self._row_to_dict(row)
    
    async def get(self, memory_id: UUID) -> dict[str, Any] | None:
        """Get a memory by ID (no access control).
        
        Args:
            memory_id: The UUID of the memory to retrieve.
            
        Returns:
            The memory record, or None if not found or deleted.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
            FROM memories
            WHERE id = $1 AND deleted_at IS NULL
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(memory_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def get_accessible(
        self,
        memory_id: UUID,
        user_id: UUID,
        organization_id: UUID,
    ) -> dict[str, Any] | None:
        """Get a memory by ID with access control.
        
        Returns the memory only if:
        - The user owns the memory, OR
        - The memory is shared within the user's organization
        
        Args:
            memory_id: The UUID of the memory to retrieve.
            user_id: The ID of the requesting user.
            organization_id: The organization of the requesting user.
            
        Returns:
            The memory record, or None if not found, deleted, or not accessible.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
            FROM memories
            WHERE id = $1 
              AND deleted_at IS NULL
              AND (
                  user_id = $2
                  OR (organization_id = $3 AND shared = true)
              )
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(memory_id), str(user_id), str(organization_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def get_individual_memory_for_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Get the individual memory associated with a user.
        
        Args:
            user_id: The user's UUID.
            
        Returns:
            The memory record, or None if not found.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
            FROM memories
            WHERE type = 'individual' 
              AND deleted_at IS NULL
              AND user_id = $1
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def set_shared(
        self,
        memory_id: UUID,
        user_id: UUID,
        shared: bool,
    ) -> dict[str, Any] | None:
        """Set the shared status of a memory.
        
        Only the owner of the memory can change its shared status.
        
        Args:
            memory_id: The UUID of the memory to update.
            user_id: The ID of the requesting user (must be owner).
            shared: Whether to share (True) or unshare (False) the memory.
            
        Returns:
            The updated memory record, or None if not found or not owned by user.
        """
        query = """
            UPDATE memories
            SET shared = $1
            WHERE id = $2 AND user_id = $3 AND deleted_at IS NULL
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata,
                      created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, shared, str(memory_id), str(user_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def update(
        self,
        memory_id: UUID,
        content: str | None = None,
        tags: list[str] | None = None,
        importance: int | None = None,
        related_memory_ids: list[UUID] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing memory.
        
        Args:
            memory_id: The UUID of the memory to update.
            content: Optional new content.
            tags: Optional new tags.
            importance: Optional new importance rating.
            related_memory_ids: Optional new related memory IDs.
            metadata: Optional new metadata.
            
        Returns:
            The updated memory record, or None if not found.
        """
        # Check if memory exists and is not deleted
        existing = await self.get(memory_id)
        if existing is None:
            return None
        
        # Validate related memory IDs if provided
        if related_memory_ids is not None:
            await self._validate_related_ids(related_memory_ids, exclude_id=memory_id)
        
        # Build dynamic update query
        updates = []
        params = []
        param_idx = 1
        
        if content is not None:
            updates.append(f"content = ${param_idx}")
            params.append(content)
            param_idx += 1
        
        if tags is not None:
            updates.append(f"tags = ${param_idx}")
            params.append(tags)
            param_idx += 1
        
        if importance is not None:
            updates.append(f"importance = ${param_idx}")
            params.append(importance)
            param_idx += 1
        
        if related_memory_ids is not None:
            updates.append(f"related_memory_ids = ${param_idx}")
            params.append([str(uid) for uid in related_memory_ids])
            param_idx += 1
        
        if metadata is not None:
            updates.append(f"metadata = ${param_idx}")
            params.append(metadata)
            param_idx += 1
        
        if not updates:
            return existing
        
        # Always increment version on update
        updates.append(f"version = version + 1")
        
        params.append(str(memory_id))
        
        query = f"""
            UPDATE memories
            SET {", ".join(updates)}
            WHERE id = ${param_idx} AND deleted_at IS NULL
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata,
                      created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at, version
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def delete(self, memory_id: UUID) -> bool:
        """Soft delete a memory by setting deleted_at timestamp.
        
        Args:
            memory_id: The UUID of the memory to delete.
            
        Returns:
            True if the memory was deleted, False if not found.
        """
        query = """
            UPDATE memories
            SET deleted_at = NOW()
            WHERE id = $1 AND deleted_at IS NULL
            RETURNING id
        """
        
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(memory_id))
        
        return result is not None
    
    async def search(
        self,
        query: str | None = None,
        username: str | None = None,
        type: str | None = None,
        tags: list[str] | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        created_after: Any | None = None,
        created_before: Any | None = None,
        memory_ids: list[UUID] | None = None,
        offset: int = 0,
        limit: int = 5,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Search for memories with fuzzy matching and filters.
        
        If access control parameters are provided, only returns:
        - Memories owned by the requesting user, OR
        - Memories shared within the requesting user's organization
        
        Args:
            query: Optional fuzzy search query for content.
            username: Optional filter by username.
            type: Optional filter by memory type.
            tags: Optional filter by tags (any match).
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            created_after: Optional filter for memories created after this date.
            created_before: Optional filter for memories created before this date.
            memory_ids: Optional filter by specific memory IDs.
            offset: Pagination offset.
            limit: Maximum results to return.
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            
        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(f"(user_id = ${param_idx} OR (organization_id = ${param_idx + 1} AND shared = true))")
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2
        
        # Build WHERE conditions
        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1
        
        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1
        
        if tags:
            conditions.append(f"tags && ${param_idx}")
            params.append(tags)
            param_idx += 1
        
        if importance_min is not None:
            conditions.append(f"importance >= ${param_idx}")
            params.append(importance_min)
            param_idx += 1
        
        if importance_max is not None:
            conditions.append(f"importance <= ${param_idx}")
            params.append(importance_max)
            param_idx += 1
        
        if created_after is not None:
            conditions.append(f"created_at >= ${param_idx}")
            params.append(created_after)
            param_idx += 1
        
        if created_before is not None:
            conditions.append(f"created_at <= ${param_idx}")
            params.append(created_before)
            param_idx += 1
        
        if memory_ids:
            placeholders = ", ".join(f"${i}" for i in range(param_idx, param_idx + len(memory_ids)))
            conditions.append(f"id IN ({placeholders})")
            params.extend(str(uid) for uid in memory_ids)
            param_idx += len(memory_ids)
        
        where_clause = " AND ".join(conditions)
        
        # Build the query with optional fuzzy matching
        if query:
            # Use pg_trgm similarity for fuzzy search
            similarity_param = param_idx
            params.append(query)
            param_idx += 1
            
            search_query = f"""
                SELECT id, username, type, content, tags, importance, related_memory_ids,
                       created_at, updated_at, user_id, organization_id, shared, last_accessed_at,
                       similarity(content, ${similarity_param}) as sim_score
                FROM memories
                WHERE {where_clause}
                  AND (content % ${similarity_param} OR content ILIKE '%' || ${similarity_param} || '%')
                ORDER BY sim_score DESC, importance DESC, created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
            
            count_query = f"""
                SELECT COUNT(*) as total
                FROM memories
                WHERE {where_clause}
                  AND (content % ${similarity_param} OR content ILIKE '%' || ${similarity_param} || '%')
            """
        else:
            search_query = f"""
                SELECT id, username, type, content, tags, importance, related_memory_ids,
                       created_at, updated_at, user_id, organization_id, shared, last_accessed_at,
                       NULL::float as sim_score
                FROM memories
                WHERE {where_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
            
            count_query = f"""
                SELECT COUNT(*) as total
                FROM memories
                WHERE {where_clause}
            """
        
        params.extend([limit, offset])
        
        async with self.pool.acquire() as conn:
            # Get total count
            count_params = params[:-2]  # Exclude limit and offset
            count_row = await conn.fetchrow(count_query, *count_params)
            total_count = count_row["total"] if count_row else 0
            
            # Get results
            rows = await conn.fetch(search_query, *params)
        
        memories = []
        for row in rows:
            content = row["content"]
            truncated = len(content) > self.TRUNCATE_LENGTH
            if truncated:
                content = content[:self.TRUNCATE_LENGTH] + "..."
            
            # Handle related_memory_ids which may be strings or UUIDs
            related_ids = []
            if row["related_memory_ids"]:
                for uid in row["related_memory_ids"]:
                    if isinstance(uid, UUID):
                        related_ids.append(uid)
                    else:
                        related_ids.append(UUID(uid))
            
            # Handle user_id
            user_id = None
            if row["user_id"]:
                user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])
            
            # Handle organization_id
            org_id = None
            if row["organization_id"]:
                org_id = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])
            
            memories.append({
                "id": row["id"],
                "username": row["username"],
                "type": row["type"],
                "content": content,
                "content_truncated": truncated,
                "tags": row["tags"],
                "importance": row["importance"],
                "related_memory_ids": related_ids,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "similarity_score": row["sim_score"],
                "user_id": user_id,
                "organization_id": org_id,
                "shared": row["shared"],
                "last_accessed_at": row["last_accessed_at"],
            })
        
        return {
            "memories": memories,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(memories) < total_count,
        }
    
    async def search_full(
        self,
        query: str,
        username: str | None = None,
        type: str | None = None,
        importance_min: int | None = None,
        importance_max: int | None = None,
        offset: int = 0,
        limit: int = 5,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Search across all text fields: content, tags, and metadata.
        
        This is a broader search that looks at all text in a memory,
        useful when you're not sure which field contains the information.
        
        If access control parameters are provided, only returns:
        - Memories owned by the requesting user, OR
        - Memories shared within the requesting user's organization
        
        Args:
            query: Search query to match against content, tags, and metadata.
            username: Optional filter by username.
            type: Optional filter by memory type.
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            offset: Pagination offset.
            limit: Maximum results to return.
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            
        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(f"(user_id = ${param_idx} OR (organization_id = ${param_idx + 1} AND shared = true))")
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2
        
        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1
        
        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1
        
        if importance_min is not None:
            conditions.append(f"importance >= ${param_idx}")
            params.append(importance_min)
            param_idx += 1
        
        if importance_max is not None:
            conditions.append(f"importance <= ${param_idx}")
            params.append(importance_max)
            param_idx += 1
        
        where_clause = " AND ".join(conditions)
        
        # Build a combined text field for searching: content + tags + metadata
        query_param = param_idx
        params.append(query)
        param_idx += 1
        
        # Search across content, array_to_string(tags), and metadata::text
        search_query = f"""
            SELECT id, username, type, content, tags, importance, related_memory_ids,
                   created_at, updated_at, user_id, organization_id, shared, last_accessed_at,
                   GREATEST(
                       similarity(content, ${query_param}),
                       similarity(array_to_string(tags, ' '), ${query_param}),
                       similarity(metadata::text, ${query_param})
                   ) as sim_score
            FROM memories
            WHERE {where_clause}
              AND (
                  content % ${query_param} OR content ILIKE '%' || ${query_param} || '%'
                  OR array_to_string(tags, ' ') % ${query_param} OR array_to_string(tags, ' ') ILIKE '%' || ${query_param} || '%'
                  OR metadata::text % ${query_param} OR metadata::text ILIKE '%' || ${query_param} || '%'
              )
            ORDER BY sim_score DESC, importance DESC, created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """
        
        count_query = f"""
            SELECT COUNT(*) as total
            FROM memories
            WHERE {where_clause}
              AND (
                  content % ${query_param} OR content ILIKE '%' || ${query_param} || '%'
                  OR array_to_string(tags, ' ') % ${query_param} OR array_to_string(tags, ' ') ILIKE '%' || ${query_param} || '%'
                  OR metadata::text % ${query_param} OR metadata::text ILIKE '%' || ${query_param} || '%'
              )
        """
        
        params.extend([limit, offset])
        
        async with self.pool.acquire() as conn:
            count_params = params[:-2]
            count_row = await conn.fetchrow(count_query, *count_params)
            total_count = count_row["total"] if count_row else 0
            
            rows = await conn.fetch(search_query, *params)
        
        memories = []
        for row in rows:
            content = row["content"]
            truncated = len(content) > self.TRUNCATE_LENGTH
            if truncated:
                content = content[:self.TRUNCATE_LENGTH] + "..."
            
            related_ids = []
            if row["related_memory_ids"]:
                for uid in row["related_memory_ids"]:
                    if isinstance(uid, UUID):
                        related_ids.append(uid)
                    else:
                        related_ids.append(UUID(uid))
            
            # Handle user_id
            user_id = None
            if row["user_id"]:
                user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])
            
            # Handle organization_id
            org_id = None
            if row["organization_id"]:
                org_id = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])
            
            memories.append({
                "id": row["id"],
                "username": row["username"],
                "type": row["type"],
                "content": content,
                "content_truncated": truncated,
                "tags": row["tags"],
                "importance": row["importance"],
                "related_memory_ids": related_ids,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "similarity_score": row["sim_score"],
                "user_id": user_id,
                "organization_id": org_id,
                "shared": row["shared"],
                "last_accessed_at": row["last_accessed_at"],
            })
        
        return {
            "memories": memories,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(memories) < total_count,
        }

    async def get_existing_tags(
        self,
        username: str | None = None,
        type: str | None = None,
        limit: int = 50,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Get existing tags with usage counts.
        
        Args:
            username: Optional filter by username.
            type: Optional filter by memory type.
            limit: Maximum number of tags to return (default 50).
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            
        Returns:
            List of {tag, count} sorted by count descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(f"(user_id = ${param_idx} OR (organization_id = ${param_idx + 1} AND shared = true))")
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2
        
        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1
        
        if type is not None:
            conditions.append(f"type = ${param_idx}")
            params.append(type)
            param_idx += 1
        
        where_clause = " AND ".join(conditions)
        
        query = f"""
            SELECT tag, COUNT(*) as count
            FROM memories, UNNEST(tags) as tag
            WHERE {where_clause}
            GROUP BY tag
            ORDER BY count DESC, tag ASC
            LIMIT ${param_idx}
        """
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        
        return [{"tag": row["tag"], "count": row["count"]} for row in rows]
    
    async def get_tag_suggestions(
        self,
        query: str,
        username: str | None = None,
        limit: int = 10,
        # Access control parameters
        requesting_user_id: UUID | None = None,
        requesting_org_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Get tag suggestions based on fuzzy matching.
        
        Args:
            query: The partial tag to search for.
            username: Optional filter by username.
            limit: Maximum number of suggestions (default 10).
            requesting_user_id: User ID for access control (if provided, enables access control).
            requesting_org_id: Organization ID for access control.
            
        Returns:
            List of {tag, count, similarity} sorted by similarity descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
        # Add access control condition if user context is provided
        if requesting_user_id is not None and requesting_org_id is not None:
            conditions.append(f"(user_id = ${param_idx} OR (organization_id = ${param_idx + 1} AND shared = true))")
            params.append(str(requesting_user_id))
            params.append(str(requesting_org_id))
            param_idx += 2
        
        if username is not None:
            conditions.append(f"username = ${param_idx}")
            params.append(username)
            param_idx += 1
        
        where_clause = " AND ".join(conditions)
        query_param = param_idx
        params.append(query.lower())
        param_idx += 1
        
        # Use trigram similarity for fuzzy matching on tags
        sql = f"""
            SELECT tag, COUNT(*) as count, similarity(tag, ${query_param}) as sim
            FROM memories, UNNEST(tags) as tag
            WHERE {where_clause}
              AND (tag % ${query_param} OR tag ILIKE '%' || ${query_param} || '%')
            GROUP BY tag
            ORDER BY sim DESC, count DESC, tag ASC
            LIMIT ${param_idx}
        """
        params.append(limit)
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        
        return [
            {"tag": row["tag"], "count": row["count"], "similarity": row["sim"]}
            for row in rows
        ]

    async def _validate_related_ids(
        self, 
        related_ids: list[UUID], 
        exclude_id: UUID | None = None
    ) -> None:
        """Validate that related memory IDs exist and are not deleted.
        
        Args:
            related_ids: List of UUIDs to validate.
            exclude_id: Optional ID to exclude from check (for self-reference prevention).
            
        Raises:
            ValueError: If any IDs are invalid, deleted, or self-referencing.
        """
        if not related_ids:
            return
        
        # Check for self-reference
        if exclude_id and exclude_id in related_ids:
            raise ValueError("A memory cannot reference itself")
        
        placeholders = ", ".join(f"${i+1}" for i in range(len(related_ids)))
        query = f"""
            SELECT id FROM memories
            WHERE id IN ({placeholders}) AND deleted_at IS NULL
        """
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *[str(uid) for uid in related_ids])
        
        # Convert found IDs to strings for comparison
        found_ids = {str(row["id"]) for row in rows}
        requested_ids = {str(uid) for uid in related_ids}
        missing_ids = requested_ids - found_ids
        
        if missing_ids:
            raise ValueError(f"Related memory IDs not found or deleted: {missing_ids}")
    
    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        # Handle related_memory_ids which may be strings or UUIDs
        related_ids = []
        if row["related_memory_ids"]:
            for uid in row["related_memory_ids"]:
                if isinstance(uid, UUID):
                    related_ids.append(uid)
                else:
                    related_ids.append(UUID(uid))
        
        # Handle user_id which may not be present in all queries
        user_id = None
        if "user_id" in row.keys() and row["user_id"]:
            user_id = row["user_id"] if isinstance(row["user_id"], UUID) else UUID(row["user_id"])
        
        # Handle organization_id which may not be present in all queries
        org_id = None
        if "organization_id" in row.keys() and row["organization_id"]:
            org_id = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])
        
        # Handle shared flag
        shared = row["shared"] if "shared" in row.keys() else False
        
        # Handle last_accessed_at
        last_accessed_at = row["last_accessed_at"] if "last_accessed_at" in row.keys() else None
        
        return {
            "id": row["id"],
            "username": row["username"],
            "type": row["type"],
            "content": row["content"],
            "tags": row["tags"],
            "importance": row["importance"],
            "related_memory_ids": related_ids,
            "metadata": row["metadata"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
            "user_id": user_id,
            "organization_id": org_id,
            "shared": shared,
            "last_accessed_at": last_accessed_at,
            "version": row["version"] if "version" in row.keys() else 1,
        }
