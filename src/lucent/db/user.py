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

from lucent.db.memory import resolve_memory_username
from lucent.db.pool import scoped_acquire
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
        role: str | None = None,
        email_verified: bool = False,
        provisioned_by: str | None = None,
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
            role: User role (member, admin, owner). Defaults to 'member'.
            email_verified: Whether the address was asserted by a trusted
                identity source. Gates email-based account linking.
            provisioned_by: How the account came to exist (reporting only).

        Returns:
            The created user record.
        """
        query = """
            INSERT INTO users (external_id, provider, organization_id,
                email, display_name, avatar_url, provider_metadata, role,
                email_verified, provisioned_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, 'member'), $9, $10)
            RETURNING id, external_id, provider, organization_id, email, display_name,
                      avatar_url, provider_metadata, is_active, created_at, updated_at,
                      last_login_at, role, email_verified, scim_external_id, provisioned_by
        """

        params = [
            external_id,
            provider,
            str(organization_id),
            email,
            display_name,
            avatar_url,
            provider_metadata or {},
            role,
            email_verified,
            provisioned_by,
        ]

        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(query, *params)

        user = self._row_to_dict(row)

        # Auto-create an individual memory for this user
        await self._create_individual_memory_for_user(user)

        return user

    async def _create_individual_memory_for_user(
        self, user: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Create an individual memory record for a user.

        This is called automatically when a user is created.

        Args:
            user: The user record.

        Returns:
            The created memory record, or None if creation failed.
        """
        name = (
            user.get("display_name")
            or user.get("email")
            or user.get("external_id")
            or "Unknown User"
        )

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

        # Attribution is resolved by the shared helper (display name -> email
        # -> id). `name` above is display text for the memory body only and
        # never reaches the username column.
        attribution = await resolve_memory_username(self.pool, user["id"])

        # Create the individual memory
        query = """
            INSERT INTO memories (username, type, content, tags,
                importance, related_memory_ids, metadata,
                user_id, organization_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id, username, type, content, tags, importance, related_memory_ids, metadata,
                      created_at, updated_at, deleted_at, user_id,
                      organization_id, last_accessed_at
        """

        try:
            async with self.pool.acquire() as conn:  # system-infra: intentionally scope-less, audited site
                row = await conn.fetchrow(
                    query,
                    attribution,  # username (derived, never display text)
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

    async def _get_individual_memory_for_user(
        self, user_id: UUID, organization_id: UUID | str | None = None
    ) -> dict[str, Any] | None:
        """Get the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            The memory record, or None if not found.
        """
        query = """
            SELECT id, username, type, content, tags, importance, related_memory_ids, metadata,
                   created_at, updated_at, deleted_at, user_id,
                   organization_id, last_accessed_at
            FROM memories
            WHERE type = 'individual'
              AND deleted_at IS NULL
              AND user_id = $1
        """

        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
            row = await conn.fetchrow(query, str(user_id))

        if row is None:
            return None

        return dict(row)

    async def get_individual_memory_for_user(
        self, user_id: UUID, organization_id: UUID | str | None = None
    ) -> dict[str, Any] | None:
        """Public method to get the individual memory associated with a user.

        Args:
            user_id: The user's UUID.

        Returns:
            The memory record, or None if not found.
        """
        return await self._get_individual_memory_for_user(user_id, organization_id)

    async def _soft_delete_individual_memory_for_user(
        self, user_id: UUID, organization_id: UUID | str | None = None
    ) -> bool:
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

        async with scoped_acquire(organization_id=organization_id, user_id=user_id) as conn:
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
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                   email_verified, scim_external_id, provisioned_by
            FROM users
            WHERE id = $1
        """

        # users is a cross-tenant identity table (no per-tenant predicate); reads/writes here bind by unique id/external_id.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, str(user_id))

        if row is None:
            return None

        return self._row_to_dict(row)

    async def get_by_email_in_organization(
        self, email: str, organization_id: UUID | str
    ) -> dict[str, Any] | None:
        """Find a member of one organization by email, case-insensitively.

        This is the identity-matching lookup for SSO and SCIM. It is scoped to
        the organization because the same person may hold accounts in several
        tenants, and matching across tenants would hand one tenant's session to
        another tenant's user.

        Daemon service identities are excluded: they share a synthetic address
        and must never be adopted by a login flow.
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                   email_verified, scim_external_id, provisioned_by
            FROM users
            WHERE organization_id = $1
              AND LOWER(email) = $2
              AND role <> 'daemon'
            LIMIT 1
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(query, str(organization_id), normalized)
        return self._row_to_dict(row) if row else None

    async def find_accounts_by_email(self, email: str) -> list[dict[str, Any]]:
        """Find every non-daemon account with this email, across all tenants.

        This is the instance-operator lookup, not an auth matcher: an
        instance-level control plane asks it before provisioning so the same
        email landing twice is a decision, not an accident. Unlike
        :meth:`get_by_email_in_organization` it is deliberately *not* scoped to
        one organization — the callers hold the instance-operator permission —
        and each result carries its organization's name and status so the
        caller can tell which tenant each account belongs to.

        Case-insensitive, matching the login lookup. Daemon service identities
        are excluded: they share a synthetic address and are never accounts a
        control plane should adopt.
        """
        normalized = (email or "").strip().lower()
        if not normalized:
            return []
        query = """
            SELECT u.id, u.external_id, u.provider, u.organization_id, u.email,
                   u.display_name, u.avatar_url, u.provider_metadata, u.is_active,
                   u.created_at, u.updated_at, u.last_login_at, u.role,
                   u.email_verified, u.scim_external_id, u.provisioned_by,
                   o.name AS organization_name, o.status AS organization_status
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            WHERE u.role <> 'daemon'
              AND LOWER(u.email) = $1
            ORDER BY u.created_at
        """

        # users is a cross-tenant identity table; this read binds by email and
        # is the instance-operator surface, so it acquires without a scope.
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, normalized)

        return [
            {
                **self._row_to_dict(row),
                "organization_name": row["organization_name"],
                "organization_status": row["organization_status"],
            }
            for row in rows
        ]

    async def find_accounts_by_display_name(
        self, display_name: str
    ) -> list[dict[str, Any]]:
        """Find every non-daemon account with this display name, across tenants.

        The username-only twin of :meth:`find_accounts_by_email`: accounts
        whose email is NULL sign in with their display name alone, so an
        instance-level lookup must be able to match on it too.

        Unlike the email lookup this is inherently ambiguous — display_name
        has no unique constraint and can repeat freely, even within one
        organization. The caller must treat more than one row as an
        unresolved conflict, never as an invitation to pick one; the route
        turns that into a 409 with the candidate list.

        Case-insensitive, matching the login lookup. Daemon service
        identities are excluded, as in the email lookup.
        """
        normalized = (display_name or "").strip().lower()
        if not normalized:
            return []
        query = """
            SELECT u.id, u.external_id, u.provider, u.organization_id, u.email,
                   u.display_name, u.avatar_url, u.provider_metadata, u.is_active,
                   u.created_at, u.updated_at, u.last_login_at, u.role,
                   u.email_verified, u.scim_external_id, u.provisioned_by,
                   o.name AS organization_name, o.status AS organization_status
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            WHERE u.role <> 'daemon'
              AND LOWER(u.display_name) = $1
            ORDER BY u.created_at
        """

        # users is a cross-tenant identity table; this read binds by display
        # name and is the instance-operator surface, so it acquires without a
        # scope, exactly as the email lookup does.
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, normalized)

        return [
            {
                **self._row_to_dict(row),
                "organization_name": row["organization_name"],
                "organization_status": row["organization_status"],
            }
            for row in rows
        ]

    async def get_by_scim_external_id(
        self, scim_external_id: str, organization_id: UUID | str
    ) -> dict[str, Any] | None:
        """Find a user by the identity provider's own SCIM identifier."""
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                   email_verified, scim_external_id, provisioned_by
            FROM users
            WHERE organization_id = $1 AND scim_external_id = $2
            LIMIT 1
        """
        async with scoped_acquire(organization_id=organization_id) as conn:
            row = await conn.fetchrow(query, str(organization_id), scim_external_id)
        return self._row_to_dict(row) if row else None

    async def set_email_verified(self, user_id: UUID, verified: bool) -> None:
        """Record whether this account's email has been asserted by a trusted source."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET email_verified = $2, updated_at = NOW() WHERE id = $1",
                str(user_id),
                verified,
            )

    async def link_sso_identity(
        self,
        user_id: UUID,
        *,
        provider: str,
        external_id: str,
        provider_metadata: dict[str, Any] | None = None,
        email_verified: bool = True,
    ) -> dict[str, Any] | None:
        """Bind an existing local account to a federated identity.

        Used when a person who already has a password account signs in through
        SSO for the first time. Only ``provider``/``external_id`` and the
        verification flag change; the account keeps its id, role, and data.

        The unique ``(provider, external_id)`` index is the collision guard: if
        two users are pointed at the same IdP subject the second update fails
        loudly rather than silently sharing a login.
        """
        query = """
            UPDATE users
            SET provider = $2,
                external_id = $3,
                provider_metadata = COALESCE($4, provider_metadata),
                email_verified = email_verified OR $5,
                updated_at = NOW()
            WHERE id = $1
            RETURNING id, external_id, provider, organization_id, email, display_name,
                      avatar_url, provider_metadata, is_active, created_at, updated_at,
                      last_login_at, role, email_verified, scim_external_id, provisioned_by
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                str(user_id),
                provider,
                external_id,
                provider_metadata,
                email_verified,
            )
        return self._row_to_dict(row) if row else None

    async def get_password_hash(self, user_id: UUID) -> str | None:
        # users is a cross-tenant identity table (no per-tenant predicate);
        # reads/writes bind by unique id. scoped_acquire is unusable here:
        # its member branch fail-closed requires an organization_id.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT password_hash FROM users WHERE id = $1",
                str(user_id),
            )
        return None if row is None else row["password_hash"]

    async def is_force_password_change(self, user_id: UUID) -> bool:
        # users is a cross-tenant identity table (no per-tenant predicate);
        # reads/writes bind by unique id. scoped_acquire is unusable here:
        # its member branch fail-closed requires an organization_id.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT force_password_change FROM users WHERE id = $1",
                str(user_id),
            )
        return bool(row and row["force_password_change"])

    async def get_by_external_id(self, external_id: str, provider: str) -> dict[str, Any] | None:
        """Get a user by their external ID and provider.

        Args:
            external_id: The ID from the auth provider.
            provider: The auth provider name.

        Returns:
            The user record, or None if not found.
        """
        query = """
            SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                   provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                   email_verified, scim_external_id, provisioned_by
            FROM users
            WHERE external_id = $1 AND provider = $2
        """

        # users is a cross-tenant identity table (no per-tenant predicate); reads/writes here bind by unique id/external_id.
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
            individual_memory = await self._get_individual_memory_for_user(
                existing["id"], existing["organization_id"]
            )
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
            SET {", ".join(updates)}, updated_at = NOW()
            WHERE id = ${param_idx}
            RETURNING id, external_id, provider, organization_id, email, display_name, avatar_url,
                      provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                      email_verified, scim_external_id, provisioned_by
        """

        # users is a cross-tenant identity table (no per-tenant predicate); reads/writes here bind by unique id/external_id.
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
        individual_memory = await self._get_individual_memory_for_user(
            user["id"], user.get("organization_id")
        )
        if not individual_memory:
            # If no individual memory exists, create one
            await self._create_individual_memory_for_user(user)
            return

        # Update the metadata with current user info
        name = (
            user.get("display_name")
            or user.get("email")
            or user.get("external_id")
            or "Unknown User"
        )

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

        # The username column is attribution, resolved from the owning user
        # record (display name -> email -> id); `name` is body text only.
        attribution = await resolve_memory_username(self.pool, user["id"])

        query = """
            UPDATE memories
            SET content = $1, metadata = $2, username = $3, updated_at = NOW()
            WHERE id = $4
        """

        try:
            async with self.pool.acquire() as conn:  # system-infra: intentionally scope-less, audited site
                await conn.execute(
                    query,
                    content,
                    current_metadata,
                    attribution,
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

        # users is a cross-tenant identity table and this write
        # touches nothing else — a raw acquire keeps login working on
        # sessions without tenant context (scoped_acquire would fail
        # closed and break every login / api-key auth).
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
                      provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                      email_verified, scim_external_id, provisioned_by
        """

        # users is a cross-tenant identity table (no per-tenant predicate);
        # a raw acquire matches update()/update_last_login() and keeps role
        # changes working on sessions without tenant context.
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, new_role, str(user_id))

        if row is None:
            return None

        user = self._row_to_dict(row)

        # Sync the individual memory with updated role
        await self._sync_individual_memory_for_user(user)

        return user

    async def transfer_ownership(
        self,
        *,
        organization_id: UUID | str,
        from_user_id: UUID | str,
        to_user_id: UUID | str,
        demote_to: str = "admin",
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Move the owner role from one member to another, atomically.

        ``idx_one_owner_per_org`` (migration 007) permits at most one ``owner``
        row per organization, so a transfer must demote *before* it promotes.
        Doing it in the caller's order — promote then demote — always fails with
        a unique violation, which is exactly what both ownership-transfer entry
        points (``POST /api/organizations/current/transfer`` and the web
        ``/settings/organization/transfer-ownership``) did. Both now route
        through here.

        The pair runs in one transaction: an instance can never be left with two
        owners or none.

        Args:
            organization_id: The organization being transferred.
            from_user_id: The current owner.
            to_user_id: The member becoming owner.
            demote_to: Role given to the outgoing owner.

        Returns:
            ``(promoted_user, demoted_user)``.
        """
        from lucent.db.pool import scoped_acquire

        promote_q = """
            UPDATE users
            SET role = 'owner', updated_at = NOW()
            WHERE id = $1 AND organization_id = $2
            RETURNING id, external_id, provider, organization_id, email, display_name,
                      avatar_url, provider_metadata, is_active, created_at, updated_at,
                      last_login_at, role, email_verified, scim_external_id,
                      provisioned_by
        """
        demote_q = """
            UPDATE users
            SET role = $3, updated_at = NOW()
            WHERE id = $1 AND organization_id = $2
            RETURNING id, external_id, provider, organization_id, email, display_name,
                      avatar_url, provider_metadata, is_active, created_at, updated_at,
                      last_login_at, role, email_verified, scim_external_id,
                      provisioned_by
        """

        async with scoped_acquire(
            organization_id=organization_id, user_id=from_user_id
        ) as conn:
            async with conn.transaction():
                demoted_row = await conn.fetchrow(
                    demote_q, str(from_user_id), str(organization_id), demote_to
                )
                promoted_row = await conn.fetchrow(
                    promote_q, str(to_user_id), str(organization_id)
                )

        demoted = self._row_to_dict(demoted_row) if demoted_row else None
        promoted = self._row_to_dict(promoted_row) if promoted_row else None

        for user in (promoted, demoted):
            if user:
                await self._sync_individual_memory_for_user(user)

        return promoted, demoted

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
                       provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                       email_verified, scim_external_id, provisioned_by
                FROM users
                WHERE organization_id = $1 AND role = $2
                ORDER BY display_name
            """
            async with scoped_acquire(organization_id=organization_id) as conn:
                rows = await conn.fetch(query, str(organization_id), role)
        else:
            query = """
                SELECT id, external_id, provider, organization_id, email, display_name, avatar_url,
                       provider_metadata, is_active, created_at, updated_at, last_login_at, role,
                       email_verified, scim_external_id, provisioned_by
                FROM users
                WHERE organization_id = $1
                ORDER BY display_name
            """
            async with scoped_acquire(organization_id=organization_id) as conn:
                rows = await conn.fetch(query, str(organization_id))

        return [self._row_to_dict(row) for row in rows]

    async def get_first_non_daemon_owner(self, organization_id: UUID) -> dict[str, Any] | None:
        async with scoped_acquire(organization_id=organization_id, role="member") as conn:
            row = await conn.fetchrow(
                """SELECT id::text, role
                   FROM users
                   WHERE organization_id = $1
                     AND role <> 'daemon'
                     AND COALESCE(external_id, '') NOT LIKE 'daemon-service:%'
                   ORDER BY CASE role
                       WHEN 'owner' THEN 0
                       WHEN 'admin' THEN 1
                       ELSE 2
                   END, created_at ASC
                   LIMIT 1""",
                organization_id,
            )
        return dict(row) if row else None

    async def get_display_names_by_ids(
        self,
        user_ids: list[UUID],
        *,
        organization_id: str | UUID,
    ) -> dict[str, str]:
        if not user_ids:
            return {}
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                "SELECT id, COALESCE(display_name, email, 'Unknown user') AS owner_name "
                "FROM users WHERE id = ANY($1::uuid[])",
                user_ids,
            )
        return {str(row["id"]): str(row["owner_name"]) for row in rows}

    async def active_user_exists(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> bool:
        async with scoped_acquire(organization_id=organization_id) as conn:
            return bool(
                await conn.fetchval(
                    """SELECT 1 FROM users
                       WHERE id = $1::uuid
                         AND organization_id = $2::uuid
                         AND is_active = true""",
                    UUID(user_id),
                    UUID(organization_id),
                )
            )

    async def delete(self, user_id: UUID) -> bool:
        """Permanently delete a user.

        Note: This will first soft-delete the associated individual memory,
        then permanently delete the user record.

        Args:
            user_id: The internal UUID of the user.

        Returns:
            True if the user was deleted, False if not found.
        """
        # First, soft-delete the associated individual memory. The user's
        # own organization_id scopes that write.
        user = await self.get_by_id(user_id)
        if user is None:
            return False
        await self._soft_delete_individual_memory_for_user(
            user_id, user.get("organization_id")
        )

        query = """
            DELETE FROM users
            WHERE id = $1
            RETURNING id
        """

        # users is a cross-tenant identity table (no per-tenant predicate);
        # raw acquire matches update()/update_role() — the individual-memory
        # soft-delete above already ran under the user's org context.
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow(query, str(user_id))

        return result is not None

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        # Handle organization_id which may be a string or UUID
        org_id = None
        if "organization_id" in row.keys() and row["organization_id"]:
            org_id = (
                row["organization_id"]
                if isinstance(row["organization_id"], UUID)
                else UUID(row["organization_id"])
            )

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
            # Identity-provisioning fields are only present on projections that
            # select them. Defaulting here keeps older queries working without
            # pretending an unselected column is false.
            "email_verified": row["email_verified"]
            if "email_verified" in row.keys()
            else False,
            "scim_external_id": row["scim_external_id"]
            if "scim_external_id" in row.keys()
            else None,
            "provisioned_by": row["provisioned_by"]
            if "provisioned_by" in row.keys()
            else None,
        }
