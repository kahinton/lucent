"""Organization repository for Lucent.

Handles organization CRUD operations.

Tenant lifecycle beyond simple CRUD (creating the first admin, minting their
API key, seeding built-ins) lives in :mod:`lucent.services.org_provisioning`
so both the hyperadmin API and the standalone local console share one
implementation.
"""

from typing import Any
from uuid import UUID

import asyncpg
from asyncpg import Pool

from lucent.db.pool import runner_guc_preamble, scoped_acquire, tenant_guc_scrub
from lucent.logging import get_logger

logger = get_logger(__name__)


class OrganizationNameConflictError(Exception):
    """Raised when an organization name is already taken (case-insensitive)."""

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"Organization name already exists: {name}")


class OrganizationRepository:
    """Repository for organization CRUD operations."""

    def __init__(self, pool: Pool):
        self.pool = pool

    async def list_real_organization_ids(self, *, system_org_name: str) -> list[str]:
        """Return IDs of user-facing organizations with at least one user."""
        async with self.pool.acquire() as conn:
            await conn.execute(runner_guc_preamble())
            try:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT o.id
                    FROM organizations o
                    JOIN users u ON u.organization_id = o.id
                    WHERE o.name <> $1
                    """,
                    system_org_name,
                )
            finally:
                await conn.execute(tenant_guc_scrub())
        return [str(row["id"]) for row in rows]

    async def create(self, name: str) -> dict[str, Any]:
        """Create a new organization.

        Every real organization is provisioned with its own ``daemon-service``
        system user in the same transaction, so the org is self-sufficient from
        the moment it is created: a daemon bound to this org has an identity to
        authenticate as, register instances under, and seed system schedules
        with — all scoped to this org. The hidden system org
        (``__lucent_system__``, used only for secret storage) is excluded; it
        never gets a daemon-service user.

        Args:
            name: The organization name.

        Returns:
            The created organization record.

        Raises:
            OrganizationNameConflictError: If the name is already taken. The
                uniqueness index (migration 149) makes this authoritative —
                callers no longer need a check-then-insert.
        """
        # Imported lazily to avoid a module-load cycle (builtin_definitions
        # pulls in higher-level modules).
        from lucent.builtin_definitions import SYSTEM_ORG_NAME

        query = """
            INSERT INTO organizations (name)
            VALUES ($1)
            RETURNING id, name, status, suspended_at, created_at, updated_at
        """

        # system-infra: intentionally scope-less, audited site
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    row = await conn.fetchrow(query, name)
                except asyncpg.UniqueViolationError as exc:
                    raise OrganizationNameConflictError(name) from exc
                org = self._row_to_dict(row)

                if name != SYSTEM_ORG_NAME:
                    # Per-org daemon-service identity. The external_id is scoped
                    # by org id so the global UNIQUE(provider, external_id)
                    # constraint allows one per organization.
                    await conn.execute(
                        """
                        INSERT INTO users
                            (external_id, provider, organization_id,
                             email, display_name, role)
                        VALUES ($1, 'local', $2, 'daemon@lucent.local',
                                'Lucent Daemon', 'daemon')
                        ON CONFLICT (provider, external_id) DO NOTHING
                        """,
                        f"daemon-service:{org['id']}",
                        str(org["id"]),
                    )

        return org

    async def get_by_id(self, org_id: UUID) -> dict[str, Any] | None:
        """Get an organization by ID.

        Args:
            org_id: The UUID of the organization.

        Returns:
            The organization record, or None if not found.
        """
        query = """
            SELECT id, name, status, suspended_at, created_at, updated_at
            FROM organizations
            WHERE id = $1
        """

        async with scoped_acquire(organization_id=org_id) as conn:
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
            SELECT id, name, status, suspended_at, created_at, updated_at
            FROM organizations
            WHERE LOWER(name) = LOWER($1)
        """

        # system-infra: intentionally scope-less, audited site
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

        try:
            new_org = await self.create(name)
        except OrganizationNameConflictError:
            # Lost a race with a concurrent creator: the name now exists, so
            # adopt the winner rather than surfacing a spurious conflict.
            raced = await self.get_by_name(name)
            if raced is None:
                raise
            return raced, False
        return new_org, True

    async def update(self, org_id: UUID, name: str) -> dict[str, Any] | None:
        """Update an organization's name.

        Args:
            org_id: The UUID of the organization.
            name: The new name.

        Returns:
            The updated organization record, or None if not found.

        Raises:
            OrganizationNameConflictError: If another organization already uses
                the name (case-insensitively).
        """
        query = """
            UPDATE organizations
            SET name = $1
            WHERE id = $2
            RETURNING id, name, status, suspended_at, created_at, updated_at
        """

        async with scoped_acquire(organization_id=org_id) as conn:
            try:
                row = await conn.fetchrow(query, name, str(org_id))
            except asyncpg.UniqueViolationError as exc:
                raise OrganizationNameConflictError(name) from exc

        if row is None:
            return None

        return self._row_to_dict(row)

    async def set_suspended(
        self,
        org_id: UUID,
        *,
        suspended: bool,
    ) -> dict[str, Any] | None:
        """Suspend or reactivate an organization without deleting tenant data."""
        query = """
            UPDATE organizations
            SET status = $1,
                suspended_at = CASE WHEN $1 = 'suspended' THEN NOW() ELSE NULL END
            WHERE id = $2
            RETURNING id, name, status, suspended_at, created_at, updated_at
        """
        new_status = "suspended" if suspended else "active"
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(query, new_status, str(org_id))

        return self._row_to_dict(row) if row else None

    async def delete(self, org_id: UUID) -> bool:
        """Permanently delete an organization.

        This is *not* a single ``DELETE``: ten tables reference
        ``organizations(id)`` with ``ON DELETE NO ACTION``, so Postgres refuses
        the delete while any of them has rows. Before this method cleared them
        first, ``DELETE /api/organizations`` (the owner's own "delete workspace"
        and every hyperadmin purge) failed with a foreign-key violation for any
        tenant that had ever run work.

        The clear-then-delete runs in one transaction, so a partially purged
        tenant can never be left behind.

        Args:
            org_id: The UUID of the organization.

        Returns:
            True if deleted, False if not found.
        """
        # Child order matters: children before parents. Keep in sync with the
        # ``confdeltype IN ('a','r')`` set on organizations (tests/conftest.py
        # asserts the same list, so a new restrictive FK fails loudly there).
        restrictive_tables = (
            "task_live_output",
            "task_outputs",
            "reviews",
            "tasks",
            "requests",
            "schedules",
            "sandboxes",
            "sandbox_templates",
            "mcp_server_configs",
            "agent_definitions",
            "skill_definitions",
            "models",
        )

        query = """
            DELETE FROM organizations
            WHERE id = $1
            RETURNING id
        """

        async with scoped_acquire(organization_id=org_id) as conn:
            async with conn.transaction():
                for table in restrictive_tables:
                    await conn.execute(
                        f"DELETE FROM {table} WHERE organization_id = $1",
                        str(org_id),
                    )
                result = await conn.fetchrow(query, str(org_id))

        return result is not None

    async def list_all(
        self,
        limit: int = 100,
        offset: int = 0,
        organization_id: Any | None = None,
    ) -> list[dict[str, Any]]:
        """List organizations, optionally filtered by ID.

        Args:
            limit: Maximum number to return.
            offset: Pagination offset.
            organization_id: If provided, only return this org (for non-admin callers).

        Returns:
            List of organization records.
        """
        if organization_id:
            query = """
                SELECT id, name, status, suspended_at, created_at, updated_at
                FROM organizations
                WHERE id = $1
                ORDER BY name ASC
                LIMIT $2 OFFSET $3
            """
            async with scoped_acquire(organization_id=organization_id) as conn:
                rows = await conn.fetch(query, str(organization_id), limit, offset)
        else:
            query = """
                SELECT id, name, status, suspended_at, created_at, updated_at
                FROM organizations
                ORDER BY name ASC
                LIMIT $1 OFFSET $2
            """
            async with scoped_acquire(organization_id=organization_id) as conn:
                rows = await conn.fetch(query, limit, offset)

        return [self._row_to_dict(row) for row in rows]

    async def list(
        self,
        offset: int = 0,
        limit: int = 20,
        *,
        status: str | None = None,
        search: str | None = None,
        exclude_names: list[str] | None = None,
        include_stats: bool = False,
    ) -> dict[str, Any]:
        """List organizations with pagination and optional filters.

        Args:
            offset: Pagination offset.
            limit: Maximum number to return.
            status: Only return organizations with this lifecycle status.
            search: Case-insensitive substring match on the name.
            exclude_names: Organization names to omit (e.g. the internal
                ``__lucent_system__`` row, which is infrastructure, not a
                tenant an operator can manage).
            include_stats: When true, each row carries ``user_count`` and
                ``admin_count`` so a list view does not need N+1 queries.

        Returns:
            Dict with organizations list and pagination info.
        """
        where: list[str] = []
        params: list[Any] = []

        if exclude_names:
            params.append(list(exclude_names))
            where.append(f"o.name <> ALL(${len(params)}::text[])")
        if status:
            params.append(status)
            where.append(f"o.status = ${len(params)}")
        if search:
            params.append(f"%{search}%")
            where.append(f"o.name ILIKE ${len(params)}")

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        if include_stats:
            select_fields = """
                o.id, o.name, o.status, o.suspended_at, o.created_at, o.updated_at,
                COALESCE(stats.user_count, 0) AS user_count,
                COALESCE(stats.admin_count, 0) AS admin_count,
                COALESCE(keys.api_key_count, 0) AS api_key_count,
                stats.owner_names
            """
            stats_join = """
                LEFT JOIN (
                    SELECT organization_id,
                           COUNT(*) FILTER (WHERE role <> 'daemon') AS user_count,
                           COUNT(*) FILTER (
                               WHERE role IN ('admin', 'owner', 'hyperadmin')
                           ) AS admin_count,
                           ARRAY_AGG(display_name ORDER BY created_at)
                               FILTER (WHERE role = 'owner' AND is_active) AS owner_names
                    FROM users
                    GROUP BY organization_id
                ) stats ON stats.organization_id = o.id
                LEFT JOIN (
                    SELECT organization_id, COUNT(*) AS api_key_count
                    FROM api_keys
                    WHERE is_active
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > NOW())
                    GROUP BY organization_id
                ) keys ON keys.organization_id = o.id
            """
        else:
            select_fields = (
                "o.id, o.name, o.status, o.suspended_at, o.created_at, o.updated_at"
            )
            stats_join = ""

        count_query = f"SELECT COUNT(*) AS total FROM organizations o {where_sql}"
        list_query = f"""
            SELECT {select_fields}
            FROM organizations o
            {stats_join}
            {where_sql}
            ORDER BY o.name ASC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """

        # system-infra: intentionally scope-less, audited site
        async with self.pool.acquire() as conn:
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row["total"] if count_row else 0
            rows = await conn.fetch(list_query, *params, limit, offset)

        organizations = []
        for row in rows:
            record = self._row_to_dict(row)
            if include_stats:
                record["user_count"] = row["user_count"]
                record["admin_count"] = row["admin_count"]
                record["api_key_count"] = row["api_key_count"]
                record["owner_names"] = list(row["owner_names"] or [])
            organizations.append(record)

        return {
            "organizations": organizations,
            "total_count": total_count,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total_count,
        }

    async def get_stats(self, org_id: UUID) -> dict[str, Any]:
        """Return member/key counts for one organization.

        Used by the organization detail view so an operator can see at a glance
        whether a tenant is provisioned (has an admin) and in use (has keys).
        """
        query = """
            SELECT
                COUNT(*) FILTER (WHERE role <> 'daemon') AS user_count,
                COUNT(*) FILTER (WHERE role IN ('admin', 'owner', 'hyperadmin'))
                    AS admin_count,
                COUNT(*) FILTER (WHERE role = 'owner' AND is_active) AS owner_count
            FROM users
            WHERE organization_id = $1 AND is_active
        """
        key_query = """
            SELECT COUNT(*) AS api_key_count
            FROM api_keys
            WHERE organization_id = $1
              AND is_active
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
        """
        async with scoped_acquire(organization_id=org_id) as conn:
            row = await conn.fetchrow(query, str(org_id))
            key_row = await conn.fetchrow(key_query, str(org_id))

        return {
            "user_count": int(row["user_count"] or 0) if row else 0,
            "admin_count": int(row["admin_count"] or 0) if row else 0,
            "owner_count": int(row["owner_count"] or 0) if row else 0,
            "api_key_count": int(key_row["api_key_count"] or 0) if key_row else 0,
        }

    def _row_to_dict(self, row: asyncpg.Record) -> dict[str, Any]:
        """Convert a database row to a dictionary."""
        return {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "suspended_at": row["suspended_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
