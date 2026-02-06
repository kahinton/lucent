"""Organization repository for Lucent.

Handles organization CRUD operations.
"""

from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool


class OrganizationRepository:
    """Repository for organization CRUD operations."""
    
    def __init__(self, pool: Pool):
        self.pool = pool
    
    async def create(self, name: str) -> dict[str, Any]:
        """Create a new organization.
        
        Args:
            name: The organization name.
            
        Returns:
            The created organization record.
        """
        query = """
            INSERT INTO organizations (name)
            VALUES ($1)
            RETURNING id, name, created_at, updated_at
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name)
        
        return self._row_to_dict(row)
    
    async def get_by_id(self, org_id: UUID) -> dict[str, Any] | None:
        """Get an organization by ID.
        
        Args:
            org_id: The UUID of the organization.
            
        Returns:
            The organization record, or None if not found.
        """
        query = """
            SELECT id, name, created_at, updated_at
            FROM organizations
            WHERE id = $1
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(org_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Get an organization by name.
        
        Args:
            name: The organization name.
            
        Returns:
            The organization record, or None if not found.
        """
        query = """
            SELECT id, name, created_at, updated_at
            FROM organizations
            WHERE name = $1
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name)
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def get_or_create(self, name: str) -> tuple[dict[str, Any], bool]:
        """Get an existing organization or create a new one.
        
        Args:
            name: The organization name.
            
        Returns:
            Tuple of (organization record, was_created boolean).
        """
        existing = await self.get_by_name(name)
        if existing:
            return existing, False
        
        new_org = await self.create(name)
        return new_org, True
    
    async def update(self, org_id: UUID, name: str) -> dict[str, Any] | None:
        """Update an organization's name.
        
        Args:
            org_id: The UUID of the organization.
            name: The new name.
            
        Returns:
            The updated organization record, or None if not found.
        """
        query = """
            UPDATE organizations
            SET name = $1
            WHERE id = $2
            RETURNING id, name, created_at, updated_at
        """
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, name, str(org_id))
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    async def delete(self, org_id: UUID) -> bool:
        """Permanently delete an organization.
        
        Note: This will cascade delete all users and their memories.
        
        Args:
            org_id: The UUID of the organization.
            
        Returns:
            True if deleted, False if not found.
        """
        query = """
            DELETE FROM organizations
            WHERE id = $1
            RETURNING id
        """
        
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(org_id))
        
        return result is not None
    
    async def list_all(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List all organizations.
        
        Args:
            limit: Maximum number to return.
            offset: Pagination offset.
            
        Returns:
            List of organization records.
        """
        query = """
            SELECT id, name, created_at, updated_at
            FROM organizations
            ORDER BY name ASC
            LIMIT $1 OFFSET $2
        """
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, limit, offset)
        
        return [self._row_to_dict(row) for row in rows]
    
    async def list(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List all organizations with pagination.
        
        Args:
            offset: Pagination offset.
            limit: Maximum number to return.
            
        Returns:
            Dict with organizations list and pagination info.
        """
        count_query = "SELECT COUNT(*) as total FROM organizations"
        query = """
            SELECT id, name, created_at, updated_at
            FROM organizations
            ORDER BY name ASC
            LIMIT $1 OFFSET $2
        """
        
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(query, limit, offset)
        
        return {
            "organizations": [self._row_to_dict(row) for row in rows],
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }
    
    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
