"""Access-control queries for ownership and group-based resource visibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from lucent.db.pool import AuthAccessRole, scoped_acquire

RESOURCE_TABLE_MAP: dict[str, str] = {
    "agent": "agent_definitions",
    "agents": "agent_definitions",
    "skill": "skill_definitions",
    "skills": "skill_definitions",
    "mcp_server": "mcp_server_configs",
    "mcp_servers": "mcp_server_configs",
    "mcp": "mcp_server_configs",
    "hook": "hook_definitions",
    "hooks": "hook_definitions",
    "managed_tool": "managed_tool_definitions",
    "managed_tools": "managed_tool_definitions",
    "tool": "managed_tool_definitions",
    "tools": "managed_tool_definitions",
    "sandbox_template": "sandbox_templates",
    "sandbox_templates": "sandbox_templates",
    "model": "models",
    "models": "models",
    "secret": "secrets",
    "secrets": "secrets",
}

_TABLES_WITH_SCOPE = {
    "agent_definitions", "skill_definitions", "mcp_server_configs",
    "hook_definitions", "managed_tool_definitions", "sandbox_templates",
}

_TABLES_WITH_TEXT_IDS = {"models"}
_TABLES_WITH_GLOBAL_ROWS = {"models"}

# Secrets are personal: org admin/owner roles carry no blanket read/modify.
# Admins gain access only through explicit auth_clearances grants (auditable
# via the /api/access grant endpoints). Group-owned secrets keep their own
# group-admin management checks.
_TABLES_WITHOUT_ADMIN_OVERRIDE = {"secrets"}


@dataclass(frozen=True)
class AuthAccessResourceSpec:
    """Location of one resource type for table-agnostic ACL management."""

    resource_type: str
    table: str
    id_column: str = "id"
    owner_column: str = "user_id"
    organization_column: str = "organization_id"
    # The resource's auth_ids pivot column — every clearance joins on it. All
    # resources share the same name; the field exists so grant queries fail
    # loudly (missing attribute) rather than interpolating a guessed column.
    auth_id_column: str = "auth_id"


AUTH_ACCESS_RESOURCES: dict[str, AuthAccessResourceSpec] = {
    "agent": AuthAccessResourceSpec(
        "agent", "agent_definitions", owner_column="owner_user_id"
    ),
    "agents": AuthAccessResourceSpec(
        "agent", "agent_definitions", owner_column="owner_user_id"
    ),
    "memory": AuthAccessResourceSpec("memory", "memories"),
    "memories": AuthAccessResourceSpec("memory", "memories"),
    "mcp_server": AuthAccessResourceSpec(
        "mcp_server", "mcp_server_configs", owner_column="owner_user_id"
    ),
    "mcp_servers": AuthAccessResourceSpec(
        "mcp_server", "mcp_server_configs", owner_column="owner_user_id"
    ),
    "hook": AuthAccessResourceSpec("hook", "hook_definitions", owner_column="owner_user_id"),
    "hooks": AuthAccessResourceSpec("hook", "hook_definitions", owner_column="owner_user_id"),
    "model": AuthAccessResourceSpec("model", "models", owner_column="owner_user_id"),
    "models": AuthAccessResourceSpec("model", "models", owner_column="owner_user_id"),
    "project": AuthAccessResourceSpec("project", "projects"),
    "projects": AuthAccessResourceSpec("project", "projects"),
    # Sandbox *instances* are deliberately absent: an instance belongs to
    # whoever is running it (its created_by owner clearance) and is never
    # shareable — use the clearances live on sandbox_templates. Removing
    # these specs makes the grant APIs 404/422 on instance grants.
    "sandbox_template": AuthAccessResourceSpec(
        "sandbox_template", "sandbox_templates", owner_column="owner_user_id"
    ),
    "sandbox_templates": AuthAccessResourceSpec(
        "sandbox_template", "sandbox_templates", owner_column="owner_user_id"
    ),
    "schedule": AuthAccessResourceSpec("schedule", "schedules", owner_column="created_by"),
    "schedules": AuthAccessResourceSpec("schedule", "schedules", owner_column="created_by"),
    "secret": AuthAccessResourceSpec("secret", "secrets", owner_column="owner_user_id"),
    "secrets": AuthAccessResourceSpec("secret", "secrets", owner_column="owner_user_id"),
    "skill": AuthAccessResourceSpec(
        "skill", "skill_definitions", owner_column="owner_user_id"
    ),
    "skills": AuthAccessResourceSpec(
        "skill", "skill_definitions", owner_column="owner_user_id"
    ),
    "managed_tool": AuthAccessResourceSpec(
        "managed_tool", "managed_tool_definitions", owner_column="owner_user_id"
    ),
    "managed_tools": AuthAccessResourceSpec(
        "managed_tool", "managed_tool_definitions", owner_column="owner_user_id"
    ),
    "tool": AuthAccessResourceSpec(
        "managed_tool", "managed_tool_definitions", owner_column="owner_user_id"
    ),
    "tools": AuthAccessResourceSpec(
        "managed_tool", "managed_tool_definitions", owner_column="owner_user_id"
    ),
}


_ADMIN_ROLES = frozenset({"admin", "owner", "hyperadmin"})
_PRINCIPAL_TYPES = frozenset({"user", "group", "org"})


def normalize_auth_resource_type(resource_type: str) -> str:
    """Map a public resource name to its canonical ACL type."""
    resource_type = (resource_type or "").strip().lower()
    spec = AUTH_ACCESS_RESOURCES.get(resource_type)
    if spec is None:
        raise ValueError(f"Unsupported resource_type: {resource_type}")
    return spec.resource_type


def normalize_resource_type(resource_type: str) -> str:
    key = (resource_type or "").strip().lower()
    if key not in RESOURCE_TABLE_MAP:
        raise ValueError(f"Unsupported resource_type: {resource_type}")
    return key


class AccessControlRepository:
    """Resolve resource access by built-in, ownership, group, and org sharing."""

    _GROUP_CACHE_TTL = timedelta(seconds=5)
    _group_cache: dict[str, tuple[datetime, list[str]]] = {}

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    def invalidate_user_groups(cls, user_id: str) -> None:
        cls._group_cache.pop(user_id, None)

    async def _get_user_role(self, user_id: str, org_id: str) -> str | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                UUID(user_id),
                UUID(org_id),
            )
        return str(row["role"]) if row else None

    async def get_user_group_ids(self, user_id: str) -> list[str]:
        now = datetime.now(UTC)
        cached = self._group_cache.get(user_id)
        if cached and cached[0] > now:
            return list(cached[1])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM user_groups WHERE user_id = $1",
                UUID(user_id),
            )
        group_ids = [str(row["group_id"]) for row in rows]
        self._group_cache[user_id] = (now + self._GROUP_CACHE_TTL, group_ids)
        return group_ids

    async def can_access(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Resolve access without exposing private definition resources to org roles."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False

        group_ids = [UUID(group_id) for group_id in await self.get_user_group_ids(user_id)]
        resource_id_param = (
            resource_id if table in _TABLES_WITH_TEXT_IDS else UUID(resource_id)
        )
        org_clause = (
            "(organization_id IS NULL OR organization_id = $2)"
            if table in _TABLES_WITH_GLOBAL_ROWS
            else "organization_id = $2"
        )
        builtin_clause = "scope = 'built-in' OR " if table in _TABLES_WITH_SCOPE else ""
        org_shared_clause = (
            "OR (scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL) "
            if table in _TABLES_WITH_SCOPE
            else ""
        )
        if table == "models":
            org_shared_clause = "OR (owner_user_id IS NULL AND owner_group_id IS NULL) "
        role_override_clause = ""
        if table not in _TABLES_WITH_SCOPE and table not in _TABLES_WITHOUT_ADMIN_OVERRIDE:
            role_override_clause = "OR $5 IN ('admin', 'owner')"
        query_params: list[object] = [
            resource_id_param,
            UUID(org_id),
            UUID(user_id),
            group_ids,
        ]
        if role_override_clause:
            query_params.append(role)
        query = f"""
            SELECT EXISTS(
                SELECT 1
                FROM {table}
                WHERE id = $1
                  AND {org_clause}
                  AND (
                      {builtin_clause}owner_user_id = $3
                      OR owner_group_id = ANY($4::uuid[])
                      {org_shared_clause}
                      {role_override_clause}
                  )
            ) AS allowed
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *query_params)
        return bool(row["allowed"]) if row else False

    async def can_modify(
        self, user_id: str, resource_type: str, resource_id: str, org_id: str
    ) -> bool:
        """Check write access without granting org roles private definition control."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return False
        if (
            role in ("admin", "owner")
            and table not in _TABLES_WITH_SCOPE
            and table not in _TABLES_WITHOUT_ADMIN_OVERRIDE
        ):
            async with self.pool.acquire() as conn:
                resource_id_param = (
                    resource_id if table in _TABLES_WITH_TEXT_IDS else UUID(resource_id)
                )
                org_clause = (
                    "(organization_id IS NULL OR organization_id = $2)"
                    if table in _TABLES_WITH_GLOBAL_ROWS
                    else "organization_id = $2"
                )
                row = await conn.fetchrow(
                    f"SELECT EXISTS("
                    f"SELECT 1 FROM {table} WHERE id = $1 AND {org_clause}"
                    f") AS e",
                    resource_id_param,
                    UUID(org_id),
                )
            return bool(row["e"]) if row else False

        group_ids = [UUID(group_id) for group_id in await self.get_user_group_ids(user_id)]
        resource_id_param = (
            resource_id if table in _TABLES_WITH_TEXT_IDS else UUID(resource_id)
        )
        org_clause = (
            "(organization_id IS NULL OR organization_id = $2)"
            if table in _TABLES_WITH_GLOBAL_ROWS
            else "organization_id = $2"
        )
        org_shared_write_clause = ""
        if table in _TABLES_WITH_SCOPE:
            org_shared_write_clause = (
                " OR ($5 IN ('admin', 'owner')"
                " AND scope = 'instance'"
                " AND owner_user_id IS NULL"
                " AND owner_group_id IS NULL)"
            )
        query_params: list[object] = [
            resource_id_param,
            UUID(org_id),
            UUID(user_id),
            group_ids,
        ]
        if org_shared_write_clause:
            query_params.append(role)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT EXISTS("
                f"SELECT 1 FROM {table}"
                f" WHERE id = $1 AND {org_clause}"
                f" AND (owner_user_id = $3 OR owner_group_id IN ("
                f"SELECT group_id FROM user_groups"
                f" WHERE user_id = $3 AND role = 'admin'"
                f" AND group_id = ANY($4::uuid[]))"
                f"{org_shared_write_clause})"
                f") AS e",
                *query_params,
            )
        return bool(row["e"]) if row else False

    async def list_accessible(
        self, user_id: str, resource_type: str, org_id: str
    ) -> list[str]:
        """Return IDs of all resources of this type the user can access."""
        normalized = normalize_resource_type(resource_type)
        table = RESOURCE_TABLE_MAP[normalized]
        role = await self._get_user_role(user_id, org_id)
        if role is None:
            return []

        group_ids = [UUID(group_id) for group_id in await self.get_user_group_ids(user_id)]
        org_clause = (
            "(organization_id IS NULL OR organization_id = $1)"
            if table in _TABLES_WITH_GLOBAL_ROWS
            else "organization_id = $1"
        )
        builtin_clause = "scope = 'built-in' OR " if table in _TABLES_WITH_SCOPE else ""
        org_shared_clause = (
            "OR (scope = 'instance' AND owner_user_id IS NULL AND owner_group_id IS NULL) "
            if table in _TABLES_WITH_SCOPE
            else ""
        )
        if table == "models":
            org_shared_clause = "OR (owner_user_id IS NULL AND owner_group_id IS NULL) "
        role_override_clause = ""
        if table not in _TABLES_WITH_SCOPE:
            role_override_clause = "OR $4 IN ('admin', 'owner')"
        query_params: list[object] = [UUID(org_id), UUID(user_id), group_ids]
        if role_override_clause:
            query_params.append(role)
        query = f"""
            SELECT id
            FROM {table}
            WHERE {org_clause}
              AND (
                  {builtin_clause}owner_user_id = $2
                  OR owner_group_id = ANY($3::uuid[])
                  {org_shared_clause}
                  {role_override_clause}
              )
            ORDER BY id
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *query_params)
        return [str(row["id"]) for row in rows]


class AuthAccessRepository:
    """Manage table-agnostic ACL rows keyed by each resource's auth ID."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _spec(resource_type: str) -> AuthAccessResourceSpec:
        spec = AUTH_ACCESS_RESOURCES.get((resource_type or "").strip().lower())
        if spec is None:
            raise ValueError(f"Unsupported resource_type: {resource_type}")
        return spec

    @staticmethod
    def _principal_type(grantee_type: str) -> str:
        # "org" is accepted as an alias — grants rows store principal_type
        # "org", and templates that render a row's fields back into a
        # revoke form repeat that stored spelling.
        principal_type = {
            "organization": "org",
            "org": "org",
            "user": "user",
            "group": "group",
        }.get((grantee_type or "").strip().lower())
        if principal_type not in _PRINCIPAL_TYPES:
            raise ValueError("grantee_type must be organization, user, or group")
        return principal_type

    async def _get_resource(
        self,
        conn: asyncpg.Connection,
        spec: AuthAccessResourceSpec,
        resource_id: UUID,
        organization_id: UUID,
    ) -> dict[str, Any]:
        resource = await conn.fetchrow(
            f"""
                SELECT {spec.id_column} AS resource_id,
                       {spec.auth_id_column} AS auth_id,
                       {spec.owner_column} AS owner_id,
                       {spec.organization_column} AS organization_id
                FROM {spec.table}
                WHERE {spec.id_column} = $1
                  AND {spec.organization_column} = $2
            """,
            resource_id,
            organization_id,
        )
        return dict(resource) if resource else {}

    async def _require_principal(
        self,
        conn: asyncpg.Connection,
        principal_type: str,
        principal_id: UUID,
        organization_id: UUID,
    ) -> None:
        if principal_type == "org":
            if principal_id != organization_id:
                raise ValueError("Organization grants must target the resource organization")
            return

        principal_table = "groups" if principal_type == "group" else "users"
        principal_exists = await conn.fetchval(
            f"SELECT EXISTS(SELECT 1 FROM {principal_table} "
            "WHERE id = $1 AND organization_id = $2)",
            principal_id,
            organization_id,
        )
        if not principal_exists:
            raise ValueError(
                f"{principal_type.capitalize()} not found in resource organization"
            )

    @staticmethod
    def _owner_is_manager(
        resource_owner_id: UUID | None,
        user_id: UUID,
        user_role: str | None,
    ) -> bool:
        return resource_owner_id == user_id or user_role in _ADMIN_ROLES

    @staticmethod
    def _reject_owner_principal(
        principal_type: str,
        principal_id: UUID,
        resource: dict[str, Any],
    ) -> None:
        """Grant management never touches the owner's clearance row.

        The owner column is authoritative: the auth_owner_clearance triggers
        keep an owner grant for it, and transfer_auth_owner_for_row only
        rewrites it when the owner column itself changes. Deleting that row
        here would therefore never be repaired — a silent permanent lockout
        of the resource owner.
        """
        if principal_type == "user" and principal_id == resource["owner_id"]:
            raise ValueError(
                "The resource owner's access is fixed at owner; transfer ownership instead"
            )

    async def list_grants(
        self,
        resource_type: str,
        resource_id: UUID,
        *,
        organization_id: UUID,
    ) -> list[dict[str, Any]]:
        """List every configured principal and role for one resource."""
        spec = self._spec(resource_type)
        async with scoped_acquire(organization_id=organization_id) as conn:
            rows = await conn.fetch(
                f"""
                    SELECT acl.id, acl.role, acl.principal_type, acl.principal_id,
                           acl.granted_by, acl.created_at,
                           resource.{spec.id_column} AS resource_id,
                           resource.{spec.organization_column} AS organization_id,
                           user_target.display_name AS user_display_name,
                           user_target.email AS user_email,
                           group_target.name AS group_name,
                           organization_target.name AS organization_name
                    FROM {spec.table} AS resource
                    JOIN auth_clearances acl
                      ON acl.auth_id = resource.{spec.auth_id_column}
                    LEFT JOIN users user_target
                      ON user_target.id = acl.principal_id
                     AND acl.principal_type = 'user'
                    LEFT JOIN groups group_target
                      ON group_target.id = acl.principal_id
                     AND acl.principal_type = 'group'
                    LEFT JOIN organizations organization_target
                      ON organization_target.id = acl.principal_id
                     AND acl.principal_type = 'org'
                    WHERE resource.{spec.id_column} = $1
                      AND resource.{spec.organization_column} = $2
                    ORDER BY acl.role, user_target.display_name, group_target.name
                """,
                resource_id,
                organization_id,
            )
        return [
            {**dict(row), "resource_type": spec.resource_type} for row in rows
        ]

    async def upsert_grant(
        self,
        resource_type: str,
        resource_id: UUID,
        *,
        grantee_type: str,
        grantee_id: UUID | None,
        role: AuthAccessRole | str,
        granted_by: UUID,
        organization_id: UUID,
    ) -> dict[str, Any]:
        """Replace one principal's role after validating ownership and tenancy."""
        spec = self._spec(resource_type)
        principal_type = self._principal_type(grantee_type)
        if principal_type == "org" and grantee_id is not None:
            raise ValueError("Organization grants do not take a grantee_id")
        if principal_type != "org" and grantee_id is None:
            raise ValueError(f"{grantee_type} grants require a grantee_id")

        role = AuthAccessRole(role)
        principal_id = organization_id if principal_type == "org" else UUID(str(grantee_id))
        async with scoped_acquire(organization_id=organization_id) as conn:
            resource = await self._get_resource(
                conn, spec, resource_id, organization_id
            )
            if not resource:
                raise ValueError("Resource not found in organization")

            async with conn.transaction():
                caller_role = await conn.fetchval(
                    "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                    granted_by,
                    organization_id,
                )
                if not self._owner_is_manager(
                    resource["owner_id"], granted_by, caller_role
                ):
                    raise ValueError("Only the resource owner can manage access")

                self._reject_owner_principal(principal_type, principal_id, resource)

                await self._require_principal(
                    conn, principal_type, principal_id, organization_id
                )
                await conn.execute(
                    """DELETE FROM auth_clearances
                       WHERE auth_id = $1
                         AND principal_type = $2
                         AND principal_id = $3""",
                    resource["auth_id"],
                    principal_type,
                    principal_id,
                )
                row = await conn.fetchrow(
                    """INSERT INTO auth_clearances
                           (auth_id, role, principal_type, principal_id, granted_by)
                       VALUES ($1, $2, $3, $4, $5)
                       RETURNING id, auth_id, role, principal_type, principal_id,
                                 granted_by, created_at""",
                    resource["auth_id"],
                    role.value,
                    principal_type,
                    principal_id,
                    granted_by,
                )
                assert row is not None
                return {
                    **dict(row),
                    "resource_id": resource["resource_id"],
                    "resource_type": spec.resource_type,
                    "organization_id": organization_id,
                }

    async def revoke_grant(
        self,
        resource_type: str,
        resource_id: UUID,
        *,
        grantee_type: str,
        grantee_id: UUID | None,
        granted_by: UUID,
        organization_id: UUID,
    ) -> bool:
        """Remove one principal's configured grant."""
        spec = self._spec(resource_type)
        principal_type = self._principal_type(grantee_type)
        if principal_type == "org" and grantee_id is not None:
            raise ValueError("Organization grants do not take a grantee_id")
        if principal_type != "org" and grantee_id is None:
            raise ValueError(f"{grantee_type} grants require a grantee_id")
        principal_id = organization_id if principal_type == "org" else UUID(str(grantee_id))

        async with scoped_acquire(organization_id=organization_id) as conn:
            resource = await self._get_resource(
                conn, spec, resource_id, organization_id
            )
            if not resource:
                raise ValueError("Resource not found in organization")

            async with conn.transaction():
                caller_role = await conn.fetchval(
                    "SELECT role FROM users WHERE id = $1 AND organization_id = $2",
                    granted_by,
                    organization_id,
                )
                if not self._owner_is_manager(
                    resource["owner_id"], granted_by, caller_role
                ):
                    raise ValueError("Only the resource owner can manage access")

                self._reject_owner_principal(principal_type, principal_id, resource)

                result = await conn.execute(
                    """DELETE FROM auth_clearances
                       WHERE auth_id = $1
                         AND principal_type = $2
                         AND principal_id = $3""",
                    resource["auth_id"],
                    principal_type,
                    principal_id,
                )
        return result == "DELETE 1"
