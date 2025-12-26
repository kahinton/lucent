"""Database client for Hindsight using asyncpg."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool, Connection

# Global connection pool
_pool: Pool | None = None


async def init_db(database_url: str | None = None) -> Pool:
    """Initialize the database connection pool and run migrations.
    
    Args:
        database_url: PostgreSQL connection URL. If not provided, uses DATABASE_URL env var.
        
    Returns:
        The initialized connection pool.
    """
    global _pool
    
    if _pool is not None:
        return _pool
    
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL environment variable is required")
    
    # Create the connection pool
    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=60,
        init=_init_connection,
    )
    
    # Run migrations
    await _run_migrations(_pool)
    
    return _pool


async def _init_connection(conn: Connection) -> None:
    """Initialize each connection with custom type codecs."""
    # Register UUID codec
    await conn.set_type_codec(
        'uuid',
        encoder=str,
        decoder=lambda x: UUID(x) if x else None,
        schema='pg_catalog',
    )
    # Register JSON codec for JSONB
    await conn.set_type_codec(
        'jsonb',
        encoder=json.dumps,
        decoder=json.loads,
        schema='pg_catalog',
    )


async def _run_migrations(pool: Pool) -> None:
    """Run SQL migration files in order."""
    migrations_dir = Path(__file__).parent / "migrations"
    
    if not migrations_dir.exists():
        return
    
    # Get all SQL files sorted by name
    migration_files = sorted(migrations_dir.glob("*.sql"))
    
    async with pool.acquire() as conn:
        for migration_file in migration_files:
            sql = migration_file.read_text()
            await conn.execute(sql)


async def get_pool() -> Pool:
    """Get the database connection pool.
    
    Returns:
        The active connection pool.
        
    Raises:
        RuntimeError: If the pool has not been initialized.
    """
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_db() first.")
    return _pool


async def close_db() -> None:
    """Close the database connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


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
            
        Returns:
            The created memory record.
        """
        # Validate related memory IDs exist and are not deleted
        if related_memory_ids:
            await self._validate_related_ids(related_memory_ids)
        
        query = """
            INSERT INTO memories (username, type, content, tags, importance, related_memory_ids, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata, 
                      created_at, updated_at, deleted_at
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
            )
        
        return self._row_to_dict(row)
    
    async def get(self, memory_id: UUID) -> dict[str, Any] | None:
        """Get a memory by ID.
        
        Args:
            memory_id: The UUID of the memory to retrieve.
            
        Returns:
            The memory record, or None if not found or deleted.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at
            FROM memories
            WHERE id = $1 AND deleted_at IS NULL
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(memory_id))
        
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
        
        params.append(str(memory_id))
        
        query = f"""
            UPDATE memories
            SET {", ".join(updates)}
            WHERE id = ${param_idx} AND deleted_at IS NULL
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata,
                      created_at, updated_at, deleted_at
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
    ) -> dict[str, Any]:
        """Search for memories with fuzzy matching and filters.
        
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
            
        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
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
                       created_at, updated_at,
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
                       created_at, updated_at,
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
    ) -> dict[str, Any]:
        """Search across all text fields: content, tags, and metadata.
        
        This is a broader search that looks at all text in a memory,
        useful when you're not sure which field contains the information.
        
        Args:
            query: Search query to match against content, tags, and metadata.
            username: Optional filter by username.
            type: Optional filter by memory type.
            importance_min: Optional minimum importance.
            importance_max: Optional maximum importance.
            offset: Pagination offset.
            limit: Maximum results to return.
            
        Returns:
            Search result with memories, total count, and pagination info.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
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
                   created_at, updated_at,
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
    ) -> list[dict[str, Any]]:
        """Get existing tags with usage counts.
        
        Args:
            username: Optional filter by username.
            type: Optional filter by memory type.
            limit: Maximum number of tags to return (default 50).
            
        Returns:
            List of {tag, count} sorted by count descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
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
    ) -> list[dict[str, Any]]:
        """Get tag suggestions based on fuzzy matching.
        
        Args:
            query: The partial tag to search for.
            username: Optional filter by username.
            limit: Maximum number of suggestions (default 10).
            
        Returns:
            List of {tag, count, similarity} sorted by similarity descending.
        """
        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []
        param_idx = 1
        
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
        }
