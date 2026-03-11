"""User repository for Lucent.

Handles user CRUD operations and individual memory management.

Note on Individual Memories:
Individual memories are automatically created when a user is created and
automatically soft-deleted when a user is deleted. This ensures each user
has a memory that can store information about them (preferences, working
style, etc.) that persists across conversations. The individual memory
is kept in sync when user information changes (email, display_name, role).
"""

from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool

from lucent.logging import get_logger

logger = get_logger("db.user")


class UserRepository:
    """Repository for user CRUD operations."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def create(
        self,
        external_id: str,
        provider: str,
        organization_id: UUID,
        email: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new user.

        Args:
            external_id: Unique ID from the auth provider.
            provider: Auth provider name (google, github, saml, local).
            organization_id: The organization this user belongs to.
            email: User's email address.
            display_name: User's display name.
            avatar_url: URL to user's avatar.
            provider_metadata: Provider-specific metadata.

        Returns:
            The created user record.
        """
        query = """
            INSERT INTO users (external_id, provider, organization_id, email, display_name, avatar_url, provider_metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, external_id, provider, organization_id, email, display_name, avatar_url,
                      provider_metadata, is_active, created_at, updated_at, last_login_at, role
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                external_id,
                provider,
                str(organization_id),
                email,
                display_name,
                avatar_url,
                provider_metadata or {},
            )

        user = self._row_to_dict(row)

        # Auto-create an individual memory for this user
        await self._create_individual_memory_for_user(user)

        return user

    async def _create_individual_memory_for_user(self, user: dict[str, Any]) -> dict[str, Any] | None:
        """Create an individual memory record for a user.

        This is called automatically when a user is created.

        Args:
            user: The user record.

        Returns:
            The created memory record, or None if creation failed.
        """
        name = user.get("display_name") or user.get("email") or user.get("external_id") or "Unknown User"

        # Build the individual memory content
        content = f"Individual memory for {name}."
        if user.get("email"):
            content += f" Contact: {user['email']}."

        # Build metadata (user_id is already tracked at the memory level, no need to duplicate)
        metadata = {
            "name": name,
            "role": user.get("role", "member"),
            "contact_info": {},
        }

        if user.get("email"):
            metadata["contact_info"]["email"] = user["email"]

        # Create the individual memory
        query = """
            INSERT INTO memories (username, type, content, tags, importance, related_memory_ids, metadata, user_id, organization_id, shared)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true)
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata,
                      created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at
        """

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    name,  # username
                    "individual",  # type
                    content,
                    ["team-member", "auto-generated"],  # tags
                    5,  # importance
                    [],  # related_memory_ids
                    metadata,
                    str(user["id"]),  # user_id (owner is the user themselves)
                    str(user["organization_id"]) if user.get("organization_id") else None,
                )

            if row:
                return dict(row)
        except Exception as e:
            # Log but don't fail user creation if memory creation fails
            logger.warning(f"Failed to create individual memory for user {user['id']}", exc_info=e)

        return None

    async def _get_individual_memory_for_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Get the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            The memory record, or None if not found.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at, user_id, organization_id, shared, last_accessed_at
            FROM memories
            WHERE type = 'individual'
              AND deleted_at IS NULL
              AND user_id = $1
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id))

        if row is None:
            return None

        return dict(row)

    async def get_individual_memory_for_user(self, user_id: UUID) -> dict[str, Any] | None:
        """Public method to get the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            The memory record, or None if not found.
        """
        return await self._get_individual_memory_for_user(user_id)

    async def _soft_delete_individual_memory_for_user(self, user_id: UUID) -> bool:
        """Soft delete the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            True if a memory was deleted, False if none found.
        """
        query = """
            UPDATE memories
            SET deleted_at = NOW()
            WHERE type = 'individual'
              AND deleted_at IS NULL
              AND user_id = $1
            RETURNING id
        """

        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(user_id))

        return result is not None

    async def get_by_id(self, user_id: UUID) -> dict[str, Any] | None:
        """Get a user by their internal ID.

        Args:
            user_id: The internal UUID of the user.

        Returns:
            The user record, or None if not found.
        """
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role
            FROM users
            WHERE id = $1
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_by_external_id(
        self,
        external_id: str,
        provider: str
    ) -> dict[str, Any] | None:
        """Get a user by their external ID and provider.

        Args:
            external_id: The ID from the auth provider.
            provider: The auth provider name.

        Returns:
            The user record, or None if not found.
        """
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role
            FROM users
            WHERE external_id = $1 AND provider = $2
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, external_id, provider)

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_or_create(
        self,
        external_id: str,
        provider: str,
        organization_id: UUID,
        email: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Get an existing user or create a new one.

        Args:
            external_id: Unique ID from the auth provider.
            provider: Auth provider name.
            organization_id: The organization this user belongs to.
            email: User's email address.
            display_name: User's display name.
            avatar_url: URL to user's avatar.
            provider_metadata: Provider-specific metadata.

        Returns:
            Tuple of (user record, was_created boolean).
        """
        existing = await self.get_by_external_id(external_id, provider)
        if existing:
            # Ensure individual memory exists for existing users (backfill)
            individual_memory = await self._get_individual_memory_for_user(existing["id"])
            if not individual_memory:
                await self._create_individual_memory_for_user(existing)
            return existing, False

        new_user = await self.create(
            external_id=external_id,
            provider=provider,
            organization_id=organization_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
            provider_metadata=provider_metadata,
        )
        return new_user, True

    async def update(
        self,
        user_id: UUID,
        email: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing user.

        Args:
            user_id: The internal UUID of the user.
            email: New email address.
            display_name: New display name.
            avatar_url: New avatar URL.
            provider_metadata: New provider metadata.
            is_active: New active status.

        Returns:
            The updated user record, or None if not found.
        """
        updates = []
        params = []
        param_idx = 1

        if email is not None:
            updates.append(f"email = ${param_idx}")
            params.append(email)
            param_idx += 1

        if display_name is not None:
            updates.append(f"display_name = ${param_idx}")
            params.append(display_name)
            param_idx += 1

        if avatar_url is not None:
            updates.append(f"avatar_url = ${param_idx}")
            params.append(avatar_url)
            param_idx += 1

        if provider_metadata is not None:
            updates.append(f"provider_metadata = ${param_idx}")
            params.append(provider_metadata)
            param_idx += 1

        if is_active is not None:
            updates.append(f"is_active = ${param_idx}")
            params.append(is_active)
            param_idx += 1

        if not updates:
            return await self.get_by_id(user_id)

        params.append(str(user_id))

        query = f"""
            UPDATE users
            SET {", ".join(updates)}
            WHERE id = ${param_idx}
            RETURNING id, external_id, provider, organization_id, email, display_name, avatar_url,
                      provider_metadata, is_active, created_at, updated_at, last_login_at, role
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *params)

        if row is None:
            return None

        user = self._row_to_dict(row)

        # Sync the individual memory with updated user info
        await self._sync_individual_memory_for_user(user)

        return user

    async def _sync_individual_memory_for_user(self, user: dict[str, Any]) -> None:
        """Sync the individual memory with updated user information.

        Updates the name, email, and role in the individual memory metadata.

        Args:
            user: The updated user record.
        """
        individual_memory = await self._get_individual_memory_for_user(user["id"])
        if not individual_memory:
            # If no individual memory exists, create one
            await self._create_individual_memory_for_user(user)
            return

        # Update the metadata with current user info
        name = user.get("display_name") or user.get("email") or user.get("external_id") or "Unknown User"

        current_metadata = individual_memory.get("metadata") or {}
        current_metadata["name"] = name
        current_metadata["role"] = user.get("role", "member")

        if "contact_info" not in current_metadata:
            current_metadata["contact_info"] = {}

        if user.get("email"):
            current_metadata["contact_info"]["email"] = user["email"]

        # Update the memory
        content = f"Individual memory for {name}."
        if user.get("email"):
            content += f" Contact: {user['email']}."

        query = """
            UPDATE memories
            SET content = $1, metadata = $2, username = $3, updated_at = NOW()
            WHERE id = $4
        """

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    content,
                    current_metadata,
                    name,
                    str(individual_memory["id"]),
                )
        except Exception as e:
            logger.warning(f"Failed to sync individual memory for user {user['id']}", exc_info=e)

    async def update_last_login(self, user_id: UUID) -> None:
        """Update the last login timestamp for a user.

        Args:
            user_id: The internal UUID of the user.
        """
        query = """
            UPDATE users
            SET last_login_at = NOW()
            WHERE id = $1
        """

        async with self.pool.acquire() as conn:
            await conn.execute(query, str(user_id))

    async def update_role(
        self,
        user_id: UUID,
        new_role: str,
    ) -> dict[str, Any] | None:
        """Update a user's role.

        Args:
            user_id: The internal UUID of the user.
            new_role: The new role (member, admin, owner).

        Returns:
            The updated user record, or None if not found.
        """
        query = """
            UPDATE users
            SET role = $1, updated_at = NOW()
            WHERE id = $2
            RETURNING id, external_id, provider, organization_id, email, display_name, avatar_url,
                      provider_metadata, is_active, created_at, updated_at, last_login_at, role
        """

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, new_role, str(user_id))

        if row is None:
            return None

        user = self._row_to_dict(row)

        # Sync the individual memory with updated role
        await self._sync_individual_memory_for_user(user)

        return user

    async def get_by_organization(
        self,
        organization_id: UUID,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get all users in an organization.

        Args:
            organization_id: The organization UUID.
            role: Optional filter by role.

        Returns:
            List of user records.
        """
        if role:
            query = """
                SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                       provider_metadata, is_active, created_at, updated_at, last_login_at, role
                FROM users
                WHERE organization_id = $1 AND role = $2
                ORDER BY display_name
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, str(organization_id), role)
        else:
            query = """
                SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                       provider_metadata, is_active, created_at, updated_at, last_login_at, role
                FROM users
                WHERE organization_id = $1
                ORDER BY display_name
            """
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, str(organization_id))

        return [self._row_to_dict(row) for row in rows]

    async def delete(self, user_id: UUID) -> bool:
        """Permanently delete a user.

        Note: This will first soft-delete the associated individual memory,
        then permanently delete the user record.

        Args:
            user_id: The internal UUID of the user.

        Returns:
            True if the user was deleted, False if not found.
        """
        # First, soft-delete the associated individual memory
        await self._soft_delete_individual_memory_for_user(user_id)

        query = """
            DELETE FROM users
            WHERE id = $1
            RETURNING id
        """

        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(user_id))

        return result is not None

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        # Handle organization_id which may be a string or UUID
        org_id = None
        if "organization_id" in row.keys() and row["organization_id"]:
            org_id = row["organization_id"] if isinstance(row["organization_id"], UUID) else UUID(row["organization_id"])

        return {
            "id": row["id"],
            "external_id": row["external_id"],
            "provider": row["provider"],
            "organization_id": org_id,
            "email": row["email"],
            "display_name": row["display_name"],
            "avatar_url": row["avatar_url"],
            "provider_metadata": row["provider_metadata"],
            "is_active": row["is_active"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
            "role": row["role"] if "role" in row.keys() else "member",
        }
